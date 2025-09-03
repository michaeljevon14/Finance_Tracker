from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

# -----------------------------
# CONFIGURATION
# -----------------------------
LINE_CHANNEL_ACCESS_TOKEN = "YOUR_LINE_CHANNEL_ACCESS_TOKEN"
LINE_CHANNEL_SECRET = "YOUR_LINE_CHANNEL_SECRET"
SHEET_NAME = "Finance Tracker"
SERVICE_ACCOUNT_FILE = "service_account.json"

# Flask app
app = Flask(__name__)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Google Sheets auth
scopes = ["https://www.googleapis.com/auth/spreadsheets"]
credentials = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
gc = gspread.authorize(credentials)

# Open sheets
spreadsheet = gc.open(SHEET_NAME)
transactions_sheet = spreadsheet.worksheet("Transactions")
budgets_sheet = spreadsheet.worksheet("Budgets")
categories_sheet = spreadsheet.worksheet("Categories")

# -----------------------------
# HELPER FUNCTIONS
# -----------------------------
def get_categories():
    """Return a list of valid categories."""
    return [row[0] for row in categories_sheet.get_all_values()[1:]]  # skip header

def get_budgets():
    """Return budgets as a dict {category: amount}."""
    try:
        values = budgets_sheet.get_all_values()[1:]  # skip header
        return {row[0]: float(row[1]) for row in values}
    except Exception:
        return {}

def set_budget(category, amount):
    """Set or update a budget for a category."""
    categories = get_categories()
    if category not in categories:
        return f"Category '{category}' does not exist."

    budgets = get_budgets()
    cell = None
    try:
        cell = budgets_sheet.find(category)
    except gspread.exceptions.CellNotFound:
        pass

    if cell:
        budgets_sheet.update_cell(cell.row, 2, amount)
    else:
        budgets_sheet.append_row([category, amount])

    return f"Budget for '{category}' set to {amount}."

def log_transaction(category, amount, t_type, notes=""):
    """Log a transaction in Transactions sheet."""
    today = datetime.today().strftime("%Y-%m-%d")
    transactions_sheet.append_row([today, category, amount, t_type, notes])
    return f"Logged {t_type} of {amount} for '{category}'."

def get_budget_status(year=None, month=None):
    """Return a simple budget vs expense summary for current month."""
    if year is None or month is None:
        today = datetime.today()
        year, month = today.year, today.month

    budgets = get_budgets()
    transactions = transactions_sheet.get_all_values()[1:]  # skip header

    expenses = {}
    for row in transactions:
        date_str, category, amount, t_type, *_ = row
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        if date_obj.year == year and date_obj.month == month and t_type.lower() == "expense":
            expenses[category] = expenses.get(category, 0) + float(amount)

    # Build status message
    lines = [f"Budget status for {month}/{year}:"]
    for cat, budget in budgets.items():
        spent = expenses.get(cat, 0)
        lines.append(f"- {cat}: {spent}/{budget}")

    return "\n".join(lines)

# -----------------------------
# LINE BOT CALLBACK
# -----------------------------
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except Exception as e:
        print("Error handling message:", e)
        abort(500)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    reply = "Sorry, I didn't understand."

    # Command: Set budget -> "budget Food 500"
    if text.lower().startswith("budget"):
        try:
            _, category, amount = text.split()
            reply = set_budget(category, float(amount))
        except Exception:
            reply = "Usage: budget <Category> <Amount>"

    # Command: Log expense -> "expense Food 100"
    elif text.lower().startswith("expense") or text.lower().startswith("income"):
        try:
            t_type, category, amount = text.split()
            t_type = t_type.lower()
            if category not in get_categories():
                reply = f"Category '{category}' does not exist."
            else:
                reply = log_transaction(category, float(amount), t_type)
        except Exception:
            reply = "Usage: expense/income <Category> <Amount>"

    # Command: Check budget status
    elif text.lower() == "status":
        reply = get_budget_status()

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

# -----------------------------
# RUN APP
# -----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)