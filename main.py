import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo

TIMEZONE = ZoneInfo("Asia/Taipei")

from flask import Flask, request, abort, jsonify
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    MessagingApi, Configuration, ApiClient,
    ReplyMessageRequest, TextMessage, PushMessageRequest
)
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent

import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# ===== ENV VARS =====
CHANNEL_SECRET = os.environ.get("CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.environ.get("CHANNEL_ACCESS_TOKEN")
SHEET_NAME = os.environ.get("SHEET_NAME", "Finance Tracker")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

missing = []
if not CHANNEL_SECRET:
    missing.append("CHANNEL_SECRET")
if not CHANNEL_ACCESS_TOKEN:
    missing.append("CHANNEL_ACCESS_TOKEN")
if not GOOGLE_CREDENTIALS_JSON:
    missing.append("GOOGLE_CREDENTIALS_JSON")
if missing:
    raise RuntimeError("Missing environment variables: " + ", ".join(missing))

# Google credentials
creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
gc = gspread.authorize(creds)
spreadsheet = gc.open(SHEET_NAME)

# Worksheets
transactions_sheet = spreadsheet.worksheet("Transactions")
balances_sheet = spreadsheet.worksheet("Balances")
categories_sheet = spreadsheet.worksheet("Categories")
transfers_sheet = spreadsheet.worksheet("Transfers")
monthly_recap_sheet = spreadsheet.worksheet("Monthly Recap")
monthly_report_sheet = spreadsheet.worksheet("Monthly Report")
budgeting_sheet = spreadsheet.worksheet("Budgeting")

# LINE
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# ===== CATEGORY MAPPING =====
# Load category mapping from Budgeting sheet
CATEGORY_MAPPING = {}

def load_category_mapping():
    """Load sub-category to main-category mapping from Budgeting sheet"""
    global CATEGORY_MAPPING
    try:
        values = budgeting_sheet.get_all_values()
        if len(values) <= 1:
            print("Warning: Budgeting sheet is empty or only has headers")
            return
        
        CATEGORY_MAPPING.clear()
        
        # Skip header row
        for row in values[1:]:
            if len(row) < 2:
                continue
            
            main_category = row[0].strip()
            sub_categories_str = row[1].strip()
            
            if not main_category or not sub_categories_str:
                continue
            
            # Split by comma and clean up
            sub_categories = [cat.strip().lower() for cat in sub_categories_str.split(',')]
            
            # Map each sub-category to main category
            for sub_cat in sub_categories:
                if sub_cat:
                    CATEGORY_MAPPING[sub_cat] = main_category
        
        print(f"[{datetime.now().isoformat()}] Loaded category mapping: {CATEGORY_MAPPING}")
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] Error loading category mapping: {e}")

def get_main_category(sub_category):
    """Get main category from sub-category using mapping"""
    if not sub_category:
        return "Other"
    
    sub_cat_lower = sub_category.lower().strip()
    return CATEGORY_MAPPING.get(sub_cat_lower, "Other")

# Load mapping on startup
load_category_mapping()

# ===== SHEET FUNCTIONS =====
def add_transaction(type_, amount, category, place, note="", invoice_number=""):
    """
    Add transaction with auto-mapping to main category
    Transactions structure: Date | Type | Amount | MainCategory | Category | Place | Note | Invoice
    """
    date_value = datetime.now(TIMEZONE)
    date_text = date_value.strftime("%m/%d/%Y %H:%M:%S")
    
    # Auto-detect main category
    main_category = get_main_category(category)
    
    transactions_sheet.append_row([
        date_text, 
        type_, 
        amount, 
        main_category,  # Column D
        category,       # Column E
        place,          # Column F
        note,           # Column G
        invoice_number  # Column H
    ], value_input_option="USER_ENTERED")
    
    print(f"[{datetime.now().isoformat()}] Appended transaction -> {date_text} | {type_} | {amount} | {main_category} | {category} | {place} | Invoice: {invoice_number if invoice_number else 'N/A'}")
    
    response = f"✅ NT${amount:,} {type_} ({category} → {main_category}) {'to' if type_=='Income' else 'from'} {place} saved."
    if invoice_number:
        response += f"\n🧾 Invoice: {invoice_number}"
    return response

def add_transfer(from_place, to_place, amount, note=""):
    date_value = datetime.now(TIMEZONE)
    date_text = date_value.strftime("%m/%d/%Y %H:%M:%S")
    transfers_sheet.append_row([date_text, from_place, to_place, amount, note], value_input_option="USER_ENTERED")
    print(f"[{datetime.now().isoformat()}] Appended transfer -> {date_text} | Transfer | {amount} | {from_place} -> {to_place}")
    return f"🔄 Transfer NT${amount:,} from {from_place} to {to_place} saved."

def set_balance(place, amount):
    values = balances_sheet.get_all_values()
    places = [row[0].lower() for row in values[1:]]  # skip header
    if place.lower() in places:
        row_idx = places.index(place.lower()) + 2
        balances_sheet.update_cell(row_idx, 2, amount)  # column B = Initial Balance
    else:
        balances_sheet.append_row([place.capitalize(), amount, "", ""])  # Place, Initial, Balance (formula), Net (formula)
    return f"✅ Initial balance for {place.capitalize()} set: NT${amount:,}"

def get_balance_report():
    values = balances_sheet.get_all_values()
    if len(values) <= 1:
        return "📊 No balances found."
    
    rows = values[1:]
    report = "📊 Current Balances:\n"
    for row in rows:
        if len(row) < 4:
            continue
        place, initial, balance, net = row[0], row[1], row[2], row[3]
        report += f"• {place}: NT${net}\n"
        report += f"  (Initial: {initial}, Balance: {balance})\n"
    return report

def get_categories_report():
    values = categories_sheet.get_all_values()
    if len(values) <= 1:
        return "📊 No categories found."
    
    rows = values[1:]
    
    total_income = 0
    total_expense = 0
    
    report = "📊 Categories Summary:\n\n"
    report += "📈 Income:\n"
    for row in rows:
        if len(row) < 4:
            continue
        category, income, expense, net = row[0], row[1], row[2], row[3]
        try:
            income_val = float(str(income).replace('$', '').replace(',', '')) if income else 0
            expense_val = float(str(expense).replace('$', '').replace(',', '')) if expense else 0
            
            if income_val > 0:
                report += f"  • {category}: NT${income_val:,.0f}\n"
                total_income += income_val
        except:
            continue
    
    report += f"\n💰 Total Income: NT${total_income:,.0f}\n\n"
    report += "📉 Expenses:\n"
    
    for row in rows:
        if len(row) < 4:
            continue
        category, income, expense, net = row[0], row[1], row[2], row[3]
        try:
            income_val = float(str(income).replace('$', '').replace(',', '')) if income else 0
            expense_val = float(str(expense).replace('$', '').replace(',', '')) if expense else 0
            
            if expense_val > 0:
                report += f"  • {category}: NT${expense_val:,.0f}\n"
                total_expense += expense_val
        except:
            continue
    
    report += f"\n💸 Total Expense: NT${total_expense:,.0f}\n"
    report += f"💵 Net: NT${(total_income - total_expense):,.0f}"
    
    return report

def get_monthly_recap():
    """Get detailed monthly recap from Monthly Recap sheet"""
    values = monthly_recap_sheet.get_all_values()
    
    if len(values) < 5:
        return "📅 No monthly recap available yet. Recap is generated on the 1st of each month."
    
    # Parse the recap sheet
    report = ""
    
    # Find key sections
    for i, row in enumerate(values):
        if not row or not row[0]:
            continue
            
        cell = str(row[0])
        
        # Title
        if 'RECAP' in cell.upper() and i == 0:
            report += f"📅 {cell}\n\n"
        
        # Financial Summary
        elif cell == '💰 FINANCIAL SUMMARY':
            report += "💰 Summary:\n"
            j = i + 1
            while j < len(values) and values[j][0] and '📊' not in str(values[j][0]) and '📉' not in str(values[j][0]):
                if len(values[j]) >= 2:
                    report += f"• {values[j][0]} {values[j][1]}\n"
                j += 1
            report += "\n"
        
        # Expense by Category
        elif cell == '📉 EXPENSE BY CATEGORY':
            report += "📉 Expense Breakdown:\n"
            j = i + 2  # Skip header row
            while j < len(values) and values[j][0] and '🔄' not in str(values[j][0]) and '💳' not in str(values[j][0]):
                if len(values[j]) >= 3 and values[j][0] != 'TOTAL':
                    cat = values[j][0]
                    amt = values[j][1]
                    trans = values[j][2]
                    report += f"• {cat}: {amt} ({trans} transactions)\n"
                j += 1
            report += "\n"
        
        # Transfers
        elif cell == '🔄 TRANSFERS':
            j = i + 2  # Skip header
            total_line = ""
            while j < len(values) and values[j][0] and '💳' not in str(values[j][0]) and '🧾' not in str(values[j][0]):
                if 'TOTAL' in str(values[j][0]):
                    total_line = f"🔄 Transfers: {values[j][2]}"
                j += 1
            if total_line:
                report += total_line + "\n\n"
        
        # Balances
        elif cell == '💳 BALANCES':
            j = i + 2  # Skip header
            while j < len(values) and values[j][0] and '🧾' not in str(values[j][0]) and '📋' not in str(values[j][0]):
                if 'TOTAL' in str(values[j][0]) and len(values[j]) >= 3:
                    report += f"💳 Balance: {values[j][1]} → {values[j][2]}\n\n"
                    break
                j += 1
        
        # Invoices
        elif cell == '🧾 INVOICES':
            j = i + 1
            invoice_info = []
            while j < len(values) and values[j][0] and '📋' not in str(values[j][0]):
                if len(values[j]) >= 2:
                    invoice_info.append(f"{values[j][0]} {values[j][1]}")
                j += 1
            if invoice_info:
                report += "🧾 Invoices: " + ", ".join(invoice_info) + "\n\n"
    
    report += "📋 See full details in 'Monthly Recap' sheet"
    
    return report

def get_report(year, month):
    """Get simple monthly report from Monthly Report sheet"""
    values = monthly_report_sheet.get_all_values()
    
    if len(values) <= 1:
        return f"📅 No report found for {year}-{month:02d}"
    
    target_month = f"{year}-{month:02d}"
    
    for row in values[1:]:  # Skip header
        if len(row) >= 3 and row[0] == target_month:
            income = row[1]
            expense = row[2]
            return f"📅 Report for {target_month}\n\n💰 Income: NT${income}\n📉 Expense: NT${expense}"
    
    return f"📅 No report found for {target_month}"

def reload_mapping():
    """Reload category mapping (useful for testing)"""
    load_category_mapping()
    return f"✅ Category mapping reloaded!\n{len(CATEGORY_MAPPING)} mappings loaded."

# ===== LINE CALLBACK =====
@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event: MessageEvent):
    text = (event.message.text or "").strip()
    parts = text.split()
    if not parts:
        return

    cmd = parts[0].lower()

    # ---- Transactions ----
    if cmd in ("i", "income", "e", "expense"):
        type_ = "Income" if cmd in ("i", "income") else "Expense"
        try:
            amount = int(parts[1])
        except:
            return reply_text(event.reply_token, "❌ Format: e/i amount category place [note] [inv:NUMBER]")
        
        category = (parts[2] if len(parts) > 2 else "Other").capitalize()
        place = (parts[3] if len(parts) > 3 else "Unknown").capitalize()
        
        # Parse note and invoice number
        note_parts = []
        invoice_number = ""
        
        for part in parts[4:]:
            if part.lower().startswith("inv:"):
                invoice_number = part[4:].upper()  # Extract invoice number after "inv:"
            else:
                note_parts.append(part)
        
        note = " ".join(note_parts)
        
        # Only add invoice for expenses
        if type_ == "Expense":
            return reply_text(event.reply_token, add_transaction(type_, amount, category, place, note, invoice_number))
        else:
            return reply_text(event.reply_token, add_transaction(type_, amount, category, place, note))

    # ---- Transfer ----
    elif cmd == "transfer" and len(parts) >= 4:
        from_place, to_place = parts[1].capitalize(), parts[2].capitalize()
        try:
            amount = int(parts[3])
        except:
            return reply_text(event.reply_token, "❌ Format: transfer from to amount [note]")
        note = " ".join(parts[4:]) if len(parts) > 4 else ""
        return reply_text(event.reply_token, add_transfer(from_place, to_place, amount, note))

    # ---- Balance ----
    elif cmd == "balance":
        return reply_text(event.reply_token, get_balance_report())
    elif cmd == "setbalance" and len(parts) == 3:
        place = parts[1].capitalize()
        try:
            amount = int(parts[2])
        except:
            return reply_text(event.reply_token, "❌ Format: setbalance place amount")
        return reply_text(event.reply_token, set_balance(place, amount))

    # ---- Categories ----
    elif cmd == "categories":
        return reply_text(event.reply_token, get_categories_report())

    # ---- Report ----
    elif cmd == "report":
        today = datetime.now(TIMEZONE)
        
        # Check if user wants specific month or current recap
        if len(parts) >= 2 and "-" in parts[1]:
            # Historical report: report 2024-09
            try:
                year, month = map(int, parts[1].split("-"))
            except:
                year, month = today.year, today.month
            return reply_text(event.reply_token, get_report(year, month))
        else:
            # Current monthly recap (default)
            return reply_text(event.reply_token, get_monthly_recap())

    # ---- Reload Mapping (for testing) ----
    elif cmd == "reload":
        return reply_text(event.reply_token, reload_mapping())

    # ---- Help ----
    elif cmd == "help":
        help_text = (
            "🤖 Finance Bot Commands:\n\n"
            "📌 Transactions:\n"
            "  i <amount> <category> <place> [note]\n"
            "  e <amount> <category> <place> [note] [inv:NUMBER]\n\n"
            "📌 Transfers:\n"
            "  transfer <from> <to> <amount> [note]\n\n"
            "📌 Balances:\n"
            "  balance\n"
            "  setbalance <place> <amount>\n\n"
            "📌 Reports:\n"
            "  categories\n"
            "  report (shows current monthly recap)\n"
            "  report YYYY-MM (shows historical month)\n\n"
            "📌 Other:\n"
            "  reload (refresh category mapping)\n"
            "  help"
        )
        return reply_text(event.reply_token, help_text)

    # ---- Default ----
    reply = "❌ Unknown command. Type 'help' to see available commands."
    return reply_text(event.reply_token, reply)

# ===== REPLY HELPERS =====
def reply_text(reply_token: str, message: str):
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        api.reply_message(
            ReplyMessageRequest(
                replyToken=reply_token,
                messages=[TextMessage(text=message)]
            )
        )

@app.get("/health")
def health():
    return "OK"

@app.get("/liff/scanner")
def liff_scanner():
    # Read the LIFF HTML file
    try:
        with open('liff_scanner.html', 'r', encoding='utf-8') as f:
            html_content = f.read()
        return html_content
    except:
        return "LIFF scanner not configured yet", 404

@app.post("/api/invoice/save")
def save_invoice():
    try:
        data = request.json
        
        # Extract data from LIFF
        user_id = data.get('userId')
        invoice_number = data.get('invoiceNumber')
        amount = data.get('amount')
        category = data.get('category')
        place = data.get('place')
        note = data.get('note', '')
        
        # Save transaction
        result = add_transaction('Expense', amount, category, place, note, invoice_number)
        
        # Send confirmation message via LINE
        with ApiClient(configuration) as api_client:
            api = MessagingApi(api_client)
            try:
                api.push_message(
                    PushMessageRequest(
                        to=user_id,
                        messages=[TextMessage(text=result)]
                    )
                )
            except:
                pass  # If push fails, still return success
        
        return jsonify({'success': True, 'message': result})
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)