from flask import Flask, render_template, request, jsonify, make_response, send_file
import math
import csv
import io
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
import logging

app = Flask(__name__)

# Configure logging (optional but good for debugging and production)
app.logger.setLevel(logging.DEBUG)  # Set to DEBUG to see more detailed logs
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
app.logger.addHandler(handler)


# --- Configuration for Default Tax Slabs (Used if user input is invalid) ---
# Example for a simplified progressive tax (e.g., hypothetical dividend tax)
# 'lower_threshold': the income level at which this rate begins.
# 'rate': the tax rate (as a decimal) applied to income above this threshold, up to the next threshold.
DEFAULT_TAX_SLABS = [
    {'lower_threshold': 0, 'rate': 0.00},       # Income from $0 to $9,999.99 at 0%
    {'lower_threshold': 10000, 'rate': 0.15},   # Income from $10,000 to $49,999.99 at 15%
    {'lower_threshold': 50000, 'rate': 0.20},   # Income from $50,000 to $99,999.99 at 20%
    {'lower_threshold': 100000, 'rate': 0.25}  # Income from $100,000 onwards at 25%
]
# NOTE: The client-side JavaScript also has a DEFAULT_DIVIDEND_TAX_BRACKETS
# for its D3 chart, ensure they are consistent or derive one from the other.


# --- Core Calculation Functions ---

def parse_float(value, default=0.0):
    """Safely converts a string to float, returning default on error."""
    try:
        return float(value)
    except (ValueError, TypeError):
        return default

def parse_int(value, default=1):
    """Safely converts a string to int, returning default on error."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default

def parse_dividend_tax_brackets_input(input_string):
    """
    Parses the dividend tax brackets from a string like "0:10,10000:15".
    Rates are expected as percentages (e.g., 10 for 10%).
    Returns a list of dictionaries sorted by lower_threshold.
    """
    brackets = []
    if not input_string:
        app.logger.warning("Empty dividend tax brackets input. Using default slabs.")
        return DEFAULT_TAX_SLABS

    parts = input_string.split(',')
    for part in parts:
        try:
            threshold_str, rate_str = part.split(':')
            threshold = parse_float(threshold_str)
            rate = parse_float(rate_str) / 100.0  # Convert percentage to decimal
            if threshold < 0 or rate < 0:
                raise ValueError("Threshold and rate must be non-negative.")
            # Changed key from 'upper_bound' to 'lower_threshold'
            brackets.append({'lower_threshold': threshold, 'rate': rate})
        except ValueError as e:
            app.logger.error(f"Invalid bracket format or value '{part}': {e}. Using default slabs.", exc_info=True)
            return DEFAULT_TAX_SLABS  # Return default on first error

    # Sort brackets by lower_threshold to ensure correct progressive calculation
    brackets.sort(key=lambda x: x['lower_threshold'])

    return brackets


def calculate_dividend_yield(dividend_value, current_price, yield_type="indicated", frequency=1):
    """
    Calculates the dividend yield.
    `dividend_value` is either TTM, Next Expected, or current annual.
    `frequency` is only used for 'forward' yield type.
    """
    dividend_value = parse_float(dividend_value)
    current_price = parse_float(current_price)
    frequency = parse_int(frequency)

    if dividend_value < 0 or current_price <= 0:
        return 0.0  # Return 0.0 for invalid inputs to avoid None in tables

    annualized_dividend = dividend_value

    if yield_type == 'forward':
        annualized_dividend = dividend_value * frequency
    # For 'trailing' and 'indicated', dividend_value is assumed to be already annualized/TTM

    if current_price == 0:  # Avoid division by zero
        return 0.0

    return (annualized_dividend / current_price) * 100

def calculate_after_tax_income(gross_income, tax_slabs, exemption_threshold):
    """
    Calculates after-tax income using slab-based progressive tax rates.
    tax_slabs should be a list of dicts like [{'lower_threshold': 0, 'rate': 0.00}, ...]
    """
    gross_income = parse_float(gross_income)
    exemption_threshold = parse_float(exemption_threshold)

    # Calculate the income that is subject to taxation after exemption
    taxable_income_amount = max(0, gross_income - exemption_threshold)
    total_tax_liability = 0  # Renamed for clarity
    
    # Ensure slabs are sorted by their lower_threshold
    sorted_slabs = sorted(tax_slabs, key=lambda x: x['lower_threshold'])
    
    # Iterate through each tax slab to apply progressive rates
    for i in range(len(sorted_slabs)):
        current_slab = sorted_slabs[i]
        
        # The lower bound of the current tax bracket's segment (e.g., $10,000 for the 15% bracket)
        slab_start_threshold = current_slab['lower_threshold']
        
        # The upper bound of the current tax bracket's segment
        # This is the lower_threshold of the next slab, or infinity if it's the last slab
        slab_end_threshold = float('inf')
        if i + 1 < len(sorted_slabs):
            slab_end_threshold = sorted_slabs[i+1]['lower_threshold']
        
        rate = current_slab['rate']

        # Determine the portion of the taxable income that falls within *this specific slab's range*
        # This is the crucial part for progressive calculation.
        
        # 1. How much of the *taxable_income_amount* is above the current slab's start threshold?
        income_above_start_of_slab = max(0, taxable_income_amount - slab_start_threshold)
        
        # 2. How much of that income falls *within the current slab's upper limit*?
        # This is the minimum of (income above start of slab) and (the size of the current slab band)
        # The size of the current slab band is (slab_end_threshold - slab_start_threshold)
        taxable_in_this_segment = min(income_above_start_of_slab, slab_end_threshold - slab_start_threshold)
        
        # Add the calculated tax for this segment
        total_tax_liability += taxable_in_this_segment * rate
        
        # If the taxable_income_amount is entirely covered by current or previous slabs, we can stop.
        if taxable_income_amount <= slab_end_threshold:
            break
            
    after_tax_income = gross_income - total_tax_liability  # Use total_tax_liability here
    return after_tax_income


def calculate_real_yield(nominal_yield, inflation_rate):
    """Calculates the inflation-adjusted (real) dividend yield."""
    nominal_yield = parse_float(nominal_yield)
    inflation_rate = parse_float(inflation_rate)  # This should already be a decimal (e.g., 0.03 for 3%)

    if inflation_rate <= -1:  # Prevent division by zero or negative denominator
        return None  # Inflation rate too low to calculate real yield meaningfully

    # Using the more precise Fisher Equation: Real Rate = ((1 + Nominal Rate) / (1 + Inflation Rate)) - 1
    # Convert nominal_yield percentage to decimal for calculation
    real_yield_decimal = ((1 + nominal_yield / 100) / (1 + inflation_rate)) - 1
    return real_yield_decimal * 100  # Convert back to percentage

def assess_sustainability_risks(payout_ratio, debt_to_equity, free_cash_flow, eps, annual_dividend_per_share):
    """Provides risk alerts based on sustainability metrics."""
    alerts = []
    
    payout_ratio = parse_float(payout_ratio)
    debt_to_equity = parse_float(debt_to_equity)
    free_cash_flow = parse_float(free_cash_flow)
    eps = parse_float(eps)
    annual_dividend_per_share = parse_float(annual_dividend_per_share)

    if payout_ratio > 100.0:
        alerts.append("High Risk: Payout Ratio > 100%! Company is paying more than it earns.")
    elif payout_ratio > 75.0:
        alerts.append("Warning: Payout Ratio > 75%. May be unsustainable long-term without strong growth.")

    if debt_to_equity > 1.5:  # Higher debt can mean less financial flexibility
        alerts.append("Warning: Debt-to-Equity Ratio is high, may impact future dividends.")
    
    if eps is not None and eps <= 0 and annual_dividend_per_share > 0:
        alerts.append("Critical: Earnings Per Share (EPS) is non-positive while dividends are paid. Dividends may not be sustainable.")
    elif eps is not None and annual_dividend_per_share > eps and eps > 0: # If payout ratio itself wasn't calculated correctly or for sanity check
        alerts.append("Warning: Annual Dividend Per Share exceeds EPS, indicating potential unsustainability.")

    # A very basic check: Is FCF positive and roughly covering the dividend payments?
    # This needs total dividend payout (per share * initial shares) which isn't available directly here.
    # For now, just a general warning about FCF.
    if free_cash_flow is not None and free_cash_flow < 0:
        alerts.append("Warning: Negative Free Cash Flow. Company might struggle to fund dividends from operations.")

    return alerts

# --- Main Calculation and Projection Logic ---

def calculate_all_projections(form_data):
    """
    Calculates dividend projections over a given time horizon, including DRIP,
    taxation, and sustainability assessments.
    """
    # Parse all inputs safely
    initial_shares = parse_float(form_data.get('initial_shares'))
    initial_share_price = parse_float(form_data.get('current_price'))
    
    selected_yield_type = form_data.get('yield_type', 'indicated')
    annual_dividend_indicated = parse_float(form_data.get('annual_dividend_indicated'))
    ttm_dividend = parse_float(form_data.get('ttm_dividend'))
    next_expected_dividend = parse_float(form_data.get('next_expected_dividend'))
    
    # Map frequency string to number
    dividend_frequency_str = form_data.get('dividend_frequency')
    if dividend_frequency_str == 'custom':
        payout_frequency_num = parse_int(form_data.get('custom_frequency_number'))
    else:
        frequency_map = {'monthly': 12, 'quarterly': 4, 'semi-annual': 2, 'annual': 1}
        payout_frequency_num = frequency_map.get(dividend_frequency_str, 1)

    dividend_growth_rate_pct = parse_float(form_data.get('dividend_growth_rate'))
    share_price_growth_rate_pct = parse_float(form_data.get('share_price_growth_rate'))
    time_horizon = parse_int(form_data.get('time_horizon'))
    
    # --- Get and parse tax brackets from form data ---
    tax_exemption_threshold = parse_float(form_data.get('tax_exemption_threshold', '0.00'))
    inflation_rate_pct = parse_float(form_data.get('inflation_rate'))
    
    dividend_tax_brackets_input_str = form_data.get('dividend_tax_brackets_input', '')
    parsed_tax_slabs = parse_dividend_tax_brackets_input(dividend_tax_brackets_input_str)
    # ---------------------------------------------------

    drip_enabled = form_data.get('drip_toggle') == 'on'

    payout_ratio = parse_float(form_data.get('payout_ratio'))
    debt_to_equity = parse_float(form_data.get('debt_to_equity'))
    free_cash_flow = parse_float(form_data.get('free_cash_flow'))
    eps = parse_float(form_data.get('eps'))

    # Determine which dividend value to use for initial calculations
    if selected_yield_type == 'trailing':
        current_annual_dividend_per_share = ttm_dividend
    elif selected_yield_type == 'forward':
        current_annual_dividend_per_share = next_expected_dividend * payout_frequency_num
    else:  # 'indicated' or default
        current_annual_dividend_per_share = annual_dividend_indicated

    # Initial Calculations
    nominal_yield_pct = calculate_dividend_yield(
        current_annual_dividend_per_share, initial_share_price, 
        yield_type=selected_yield_type, frequency=payout_frequency_num
    )
    # Pass inflation rate as a decimal to calculate_real_yield
    real_yield_pct = calculate_real_yield(nominal_yield_pct, inflation_rate_pct / 100)
    
    # Sustainability Alerts
    sustainability_alerts = assess_sustainability_risks(
        payout_ratio, debt_to_equity, free_cash_flow, eps, current_annual_dividend_per_share
    )

    # --- Projections Over Time ---
    projected_data = {
        'years': [],
        'shares_owned': [],
        'share_price': [],
        'annual_dividend_per_share': [],
        'annual_gross_income': [],
        'annual_after_tax_income': [],  # This will now be truly after-tax
        'cumulative_gross_income': [],
        'cumulative_after_tax_income': [],  # This will now be truly after-tax
        'nominal_yield_over_time': [],
        'real_yield_over_time': []  # This already uses inflation_rate_pct
    }

    current_shares = initial_shares
    current_share_price = initial_share_price
    current_payout_per_share = current_annual_dividend_per_share  # This is the base for growth
    
    cumulative_gross = 0  # Initialize Total Cumulative Gross Dividend Income
    cumulative_after_tax = 0

    for year in range(1, time_horizon + 1):
        # 1. Project Dividend Payout (payout-focused growth)
        if year > 1:  # Apply growth from year 2 onwards
            current_payout_per_share *= (1 + (dividend_growth_rate_pct / 100))
        
        # 2. Project Share Price
        if year > 1:  # Apply growth from year 2 onwards
            current_share_price *= (1 + (share_price_growth_rate_pct / 100))

        # 3. Calculate Annual Gross Dividend Income for the current year
        # Formula: Annual Gross Income (Current Year) = Current Shares Owned * Current Annual Dividend Per Share
        annual_gross_income = current_shares * current_payout_per_share
        
        # Apply progressive tax to get Annual After-Tax Income
        annual_after_tax_income = calculate_after_tax_income(
            annual_gross_income, parsed_tax_slabs, tax_exemption_threshold
        )

        # Accumulate Total Cumulative Gross Dividend Income
        # Formula: Cumulative Gross (Current Year) = Cumulative Gross (Previous Year) + Annual Gross Income (Current Year)
        cumulative_gross += annual_gross_income
        
        # Accumulate Total Cumulative After-Tax Dividend Income
        cumulative_after_tax += annual_after_tax_income

        # 4. DRIP Modeling (reinvest after-tax dividends)
        if drip_enabled and current_share_price > 0:  # Only if DRIP is on and price is positive
            reinvested_shares = annual_after_tax_income / current_share_price
            current_shares += reinvested_shares

        # 5. Calculate Yields for this year
        nominal_yield_this_year = calculate_dividend_yield(current_payout_per_share, current_share_price, yield_type="indicated")
        # Pass inflation rate as a decimal to calculate_real_yield
        real_yield_this_year = calculate_real_yield(nominal_yield_this_year, inflation_rate_pct / 100)

        # Store data for output/charting
        projected_data['years'].append(year)
        projected_data['shares_owned'].append(round(current_shares, 4))
        projected_data['share_price'].append(round(current_share_price, 2))
        projected_data['annual_dividend_per_share'].append(round(current_payout_per_share, 2))
        projected_data['annual_gross_income'].append(round(annual_gross_income, 2))
        projected_data['annual_after_tax_income'].append(round(annual_after_tax_income, 2))
        projected_data['cumulative_gross_income'].append(round(cumulative_gross, 2))
        projected_data['cumulative_after_tax_income'].append(round(cumulative_after_tax, 2))
        projected_data['nominal_yield_over_time'].append(round(nominal_yield_this_year, 2) if nominal_yield_this_year is not None else None)
        projected_data['real_yield_over_time'].append(round(real_yield_this_year, 2) if real_yield_this_year is not None else None)
    
    # Final total return calculation (dividends + capital gain)
    final_capital_value = current_shares * current_share_price
    # Initial capital value is initial_shares * initial_share_price
    # total_return_value should be (final_capital_value - initial_capital_value) + total_cumulative_after_tax_income
    # OR, if total return is meant to be the total value of the investment at the end, including dividends
    total_return_value = final_capital_value + cumulative_after_tax

    return {
        'initial_shares': initial_shares,  # Added for completeness in results
        'current_price': initial_share_price,  # Added for completeness in results
        'initial_nominal_yield': nominal_yield_pct,
        'initial_real_yield': real_yield_pct,
        'sustainability_alerts': sustainability_alerts,
        'projected_data': projected_data,
        'final_shares': round(current_shares, 4),
        'final_share_price': round(current_share_price, 2),
        'total_cumulative_gross_income': round(cumulative_gross, 2),
        'total_cumulative_after_tax_income': round(cumulative_after_tax, 2),
        'total_return_value': round(total_return_value, 2)
    }


# --- Flask Routes ---

@app.route('/', methods=['GET', 'POST'])
def index():
    """
    Handles the main application route, processes form submissions,
    and renders the dividend projection results.
    """
    results = None
    error_message = None

    if request.method == 'POST':
        form_data = request.form.to_dict()

        # Basic input validation for critical fields
        # Check for presence and valid numeric conversion
        required_float_fields = ['initial_shares', 'current_price', 'inflation_rate']
        for field in required_float_fields:
            if not form_data.get(field) or parse_float(form_data.get(field), -1) < 0:
                error_message = f"Please enter a valid positive number for {field.replace('_', ' ').title()}."
                return render_template('index.html', results=results, error=error_message)
        
        # Specific dividend input validation based on yield type
        selected_yield_type = form_data.get('yield_type', 'indicated')
        dividend_input_present = False
        if selected_yield_type == 'indicated' and parse_float(form_data.get('annual_dividend_indicated'), -1) >= 0:
            dividend_input_present = True
        elif selected_yield_type == 'trailing' and parse_float(form_data.get('ttm_dividend'), -1) >= 0:
            dividend_input_present = True
        elif selected_yield_type == 'forward' and parse_float(form_data.get('next_expected_dividend'), -1) >= 0:
            dividend_input_present = True
        
        if not dividend_input_present:
            error_message = "Please enter a valid non-negative dividend amount for the selected yield type."
            return render_template('index.html', results=results, error=error_message)

        # Time horizon validation
        time_horizon = parse_int(form_data.get('time_horizon'), 0)
        if time_horizon <= 0:
            error_message = "Time Horizon must be a positive integer."
            return render_template('index.html', results=results, error=error_message)
        
        try:
            results = calculate_all_projections(form_data)
        except Exception as e:
            error_message = f"An unexpected error occurred during calculation: {e}"
            app.logger.error(f"Error during projection calculation: {e}", exc_info=True)

    return render_template('index.html',
                           results=results,
                           error=error_message)

@app.route('/download_csv', methods=['POST'])
def download_csv():
    """
    Generates and serves a CSV file containing the dividend projection data.
    """
    form_data = request.form.to_dict()
    try:
        results = calculate_all_projections(form_data)
        projected_data = results['projected_data']

        si = io.StringIO()
        cw = csv.writer(si)

        # CSV Header
        headers = [
            "Year", "Shares Owned", "Share Price ($)", "Dividend/Share ($)",
            "Annual Gross Income ($)", "Annual After-Tax Income ($)",
            "Cumulative Gross Income ($)", "Cumulative After-Tax Income ($)",
            "Nominal Yield (%)", "Real Yield (%)"
        ]
        cw.writerow(headers)

        # CSV Rows
        for i in range(len(projected_data['years'])):
            row = [
                projected_data['years'][i],
                f"{projected_data['shares_owned'][i]:.4f}",
                f"{projected_data['share_price'][i]:.2f}",
                f"{projected_data['annual_dividend_per_share'][i]:.2f}",
                f"{projected_data['annual_gross_income'][i]:.2f}",
                f"{projected_data['annual_after_tax_income'][i]:.2f}",
                f"{projected_data['cumulative_gross_income'][i]:.2f}",
                f"{projected_data['cumulative_after_tax_income'][i]:.2f}",
                f"{projected_data['nominal_yield_over_time'][i]:.2f}" if projected_data['nominal_yield_over_time'][i] is not None else "N/A",
                f"{projected_data['real_yield_over_time'][i]:.2f}" if projected_data['real_yield_over_time'][i] is not None else "N/A"
            ]
            cw.writerow(row)
        
        output = si.getvalue().encode('utf-8')
        response = make_response(output)
        response.headers["Content-Disposition"] = "attachment; filename=dividend_projections.csv"
        response.headers["Content-type"] = "text/csv"
        return response

    except Exception as e:
        app.logger.error(f"Error generating CSV: {e}", exc_info=True)
        return "Error generating CSV file.", 500


@app.route('/download_pdf', methods=['POST'])
def download_pdf():
    """
    Generates a PDF document with a dividend projection report using ReportLab's
    SimpleDocTemplate and Table objects for structured content.
    """
    form_data = request.form.to_dict()
    try:
        results = calculate_all_projections(form_data)
        projected_data = results['projected_data']

        buffer = io.BytesIO()  # Use io.BytesIO directly

        # Define custom page size (16 inches width x 10 inches height)
        custom_width = 16 * inch
        custom_height = 10 * inch

        doc = SimpleDocTemplate(buffer, pagesize=(custom_width, custom_height),
                                 rightMargin=30, leftMargin=30,
                                 topMargin=30, bottomMargin=30)
        
        styles = getSampleStyleSheet()
        story = []

        title_style = styles['h1']
        title_style.alignment = 1
        title_style.fontSize = 14
        title_style.fontName = "Helvetica-Bold"
        story.append(Paragraph("Dividend Projection Report", title_style))
        story.append(Spacer(1, 0.2 * inch))

        headers = [
            "Year", "Shares Owned", "Share Price($)", "Dividend/Share($)", "Annual Gross Income($)", "Annual After-Tax Income($)",
            "Cumulative Gross Income($)", "Cumulative After-Tax Income($)", "Nominal Yield %", "Real Yield %"
        ]

        table_data = [headers]

        for i in range(len(projected_data['years'])):
            row = [
                projected_data['years'][i],
                f"{projected_data['shares_owned'][i]:.4f}",
                f"{projected_data['share_price'][i]:.2f}",
                f"{projected_data['annual_dividend_per_share'][i]:.2f}",
                f"{projected_data['annual_gross_income'][i]:.2f}",
                f"{projected_data['annual_after_tax_income'][i]:.2f}",
                f"{projected_data['cumulative_gross_income'][i]:.2f}",
                f"{projected_data['cumulative_after_tax_income'][i]:.2f}",
                f"{projected_data['nominal_yield_over_time'][i]:.2f}" if projected_data['nominal_yield_over_time'][i] is not None else "N/A",
                f"{projected_data['real_yield_over_time'][i]:.2f}" if projected_data['real_yield_over_time'][i] is not None else "N/A"
            ]
            table_data.append(row)

        available_width = custom_width - doc.leftMargin - doc.rightMargin
        
        col_widths = [
            0.04 * available_width,   # Year
            0.10 * available_width,   # Shares Owned
            0.09 * available_width,   # Share Price($)
            0.11 * available_width,   # Dividend/Share($)
            0.12 * available_width,   # Annual Gross Income($)
            0.13 * available_width,   # Annual After-Tax Income($)
            0.13 * available_width,   # Cumulative Gross Income($)
            0.14 * available_width,   # Cumulative After-Tax Income($)
            0.07 * available_width,   # Nominal Yield (%)
            0.07 * available_width    # Real Yield (%)
        ]

        table = Table(table_data, colWidths=col_widths)

        table_style = TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#D3D3D3')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.black),
            ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
            ('VALIGN', (0, 0), (-1, 0), 'MIDDLE'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 10),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.white),
            ('TEXTCOLOR', (0, 1), (-1, -1), colors.black),
            ('ALIGN', (0, 1), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 1), (-1, -1), 'MIDDLE'),
            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 1), (-1, -1), 9),
            ('TOPPADDING', (0, 1), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 1), (-1, -1), 6),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('BOX', (0, 0), (-1, -1), 1, colors.black),
        ])
        table.setStyle(table_style)
        
        story.append(table)
        doc.build(story)

        buffer.seek(0)

        return send_file(
            buffer,
            as_attachment=True,
            download_name="dividend_projections.pdf",
            mimetype="application/pdf"
        )

    except Exception as e:
        app.logger.error(f"Error generating PDF: {e}", exc_info=True)
        return "Error generating PDF file.", 500


if __name__ == "__main__":
    app.run(debug=True)