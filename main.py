import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    MessagingApi, Configuration, ApiClient,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent

import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# ----- ENV VARS -----
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

# Parse Google credentials JSON
try:
    creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
except Exception as e:
    raise RuntimeError("GOOGLE_CREDENTIALS_JSON is not valid JSON: " + str(e))

scopes = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_info(creds_info, scopes=scopes)

# Authorize gspread
gc = gspread.authorize(creds)
spreadsheet = gc.open(SHEET_NAME)

# Worksheets
transactions_sheet = spreadsheet.worksheet("Transactions")
budgets_sheet = spreadsheet.worksheet("Budgets")

# Ensure Balances sheet exists
try:
    balances_sheet = spreadsheet.worksheet("Balances")
except gspread.exceptions.WorksheetNotFound:
    balances_sheet = spreadsheet.add_worksheet(title="Balances", rows="10", cols="2")
    balances_sheet.update("A1:B1", [["Place", "Balance"]])
    for p in ["Cash", "Post", "Cathay"]:
        balances_sheet.append_row([p, 0])

# LINE API
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# ===== Categories =====
def get_categories():
    records = transactions_sheet.get_all_records()
    categories = set()
    for row in records:
        if row.get("Category"):
            categories.add(row["Category"].lower())
    budgets = get_budgets()
    categories.update(budgets.keys())
    return sorted(categories)

# ===== Balance Report (dynamic) =====
def get_balance():
    records = transactions_sheet.get_all_records()
    balances = {}  # dynamic, any place can be added

    for row in records:
        amount = row["Amount"]
        place = row["Place"].lower()
        if place not in balances:
            balances[place] = 0
        if row["Type"].lower().startswith("i"):
            balances[place] += amount
        else:
            balances[place] -= amount
    return balances

def format_balance_report(balances):
    report = "📊 Current Balances:\n"
    total = 0
    for place, amt in balances.items():
        report += f"- {place.capitalize()}: {amt} TWD\n"
        total += amt
    report += f"💰 Total: {total} TWD"
    return report

def format_balance_report(balances):
    total = sum(balances.values())
    report = "📊 Current Balances:\n"
    for place, bal in balances.items():
        report += f"- {place.capitalize()}: {bal} TWD\n"
    report += f"💰 Total: {total} TWD"
    return report

def update_balances(place, amount, type_):
    # Read current balances
    records = balances_sheet.get_all_values()[1:]  # skip header
    balances = {row[0].lower(): int(row[1]) for row in records}

    place_key = place.lower()
    if place_key not in balances:
        balances[place_key] = 0  # Add new place automatically

    # Update balance
    if type_.lower().startswith("i"):
        balances[place_key] += amount
    else:
        balances[place_key] -= amount

    # Clear and rewrite balances sheet
    balances_sheet.clear()
    balances_sheet.update("A1:B1", [["Place", "Balance"]])
    rows = [[p.capitalize(), b] for p, b in balances.items()]
    balances_sheet.update("A2", rows)

def get_monthly_report(year, month):
    records = transactions_sheet.get_all_records()
    total_income, total_expense = 0, 0

    # Prepare all categories for income and expense
    categories = get_categories()
    income_by_cat = {cat.lower(): 0 for cat in categories}
    expense_by_cat = {cat.lower(): 0 for cat in categories}

    for row in records:
        row_date = datetime.strptime(row["Date"], "%Y-%m-%d %H:%M:%S")
        if row_date.year == year and row_date.month == month:
            amount = row["Amount"]
            category = row["Category"].lower()
            if row["Type"].lower().startswith("i"):
                total_income += amount
                income_by_cat[category] = income_by_cat.get(category, 0) + amount
            else:
                total_expense += amount
                expense_by_cat[category] = expense_by_cat.get(category, 0) + amount

    net = total_income - total_expense

    # Sort categories by amount descending
    sorted_income = sorted(income_by_cat.items(), key=lambda x: x[1], reverse=True)
    sorted_expense = sorted(expense_by_cat.items(), key=lambda x: x[1], reverse=True)

    report = (
        f"📅 Report for {year}-{month:02d}\n"
        f"Total Income: {total_income} TWD\n"
        f"Total Expenses: {total_expense} TWD\n"
        f"Net Savings: {net} TWD\n\n"
        "Income by Category:\n"
    )
    for cat, amt in sorted_income:
        report += f"- {cat.capitalize()}: {amt} TWD\n"

    report += "\nExpenses by Category:\n"
    for cat, amt in sorted_expense:
        report += f"- {cat.capitalize()}: {amt} TWD\n"

    return report

# ===== Budgeting =====
def get_budgets():
    try:
        budget_sheet = spreadsheet.worksheet("Budgets")
    except gspread.exceptions.WorksheetNotFound:
        budget_sheet = spreadsheet.add_worksheet(title="Budgets", rows="100", cols="2")
        budget_sheet.update("A1:B1", [["Category", "Amount"]])
    records = budget_sheet.get_all_records()
    return {row["Category"].lower(): row["Amount"] for row in records}

def set_budget(category, amount):
    budgets = get_budgets()
    budgets[category.lower()] = amount
    budget_sheet = spreadsheet.worksheet("Budgets")
    budget_sheet.clear()
    budget_sheet.update("A1:B1", [["Category", "Amount"]])
    rows = [[cat, amt] for cat, amt in budgets.items()]
    budget_sheet.update("A2", rows)

def get_budget_status(year, month):
    budgets = get_budgets()
    records = transactions_sheet.get_all_records()
    spent = {}

    for row in records:
        row_date = datetime.strptime(row["Date"], "%Y-%m-%d %H:%M:%S")
        if row_date.year == year and row_date.month == month:
            if row["Type"].lower().startswith("e"):
                cat = row["Category"].lower()
                spent[cat] = spent.get(cat, 0) + row["Amount"]

    report = f"🎯 Budget Status ({year}-{month:02d})\n"
    for cat, limit in budgets.items():
        used = spent.get(cat, 0)
        pct = int((used / limit) * 100) if limit > 0 else 0
        report += f"- {cat.capitalize()}: {used} / {limit} TWD ({pct}%)\n"
    return report

# ===== LINE webhook =====
@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("❌ Invalid signature. Check CHANNEL_SECRET.")
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event: MessageEvent):
    text = (event.message.text or "").strip()
    parts = text.split()
    if not parts:
        return

    # Add transaction
    if parts[0].lower() in ("e", "i"):
        type_ = "Expense" if parts[0].lower() == "e" else "Income"
        try:
            amount = int(parts[1])
        except Exception:
            return reply_text(event.reply_token,
                "Format: e/i amount category place note(optional)\nex: e 500 food cash lunch")

        category = parts[2] if len(parts) > 2 else "Other"
        place = parts[3] if len(parts) > 3 else "Unknown"
        note = " ".join(parts[4:]) if len(parts) > 4 else ""

        date_str = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M:%S")
        transactions_sheet.append_row([date_str, type_, amount, category, place, note])

        # Update balances automatically
        update_balances(place, amount, type_)

        reply = f"✅ NT${amount:,} {type_} ({category}) {('from' if type_=='Expense' else 'to')} {place} saved."
        return reply_text(event.reply_token, reply)

    # Balance
    if text.lower() == "balance":
        balances = get_balance()  # now reads directly from Balances sheet
        return reply_text(event.reply_token, format_balance_report(balances))
    
    # Set initial balance or add new place
    elif parts[0].lower() == "setbalance" and len(parts) == 3:
        place, amount = parts[1], int(parts[2])
        date_str = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M:%S")
        transactions_sheet.append_row([date_str, "Income", amount, "Balance", place, "Initial"])
        return reply_text(event.reply_token, f"✅ Balance for {place} set: {amount} TWD")

    # Set balance
    elif text.lower().startswith("setbalance"):
        if len(parts) == 3:
            place = parts[1].lower()
            try:
                target_balance = int(parts[2])
            except ValueError:
                return reply_text(event.reply_token, "❌ Invalid amount. Example: setbalance cash 2000")

            balances = get_balance()
            current_balance = balances.get(place, 0)
            diff = target_balance - current_balance

            if diff == 0:
                return reply_text(event.reply_token, f"ℹ️ {place.capitalize()} balance already {target_balance} TWD.")

            # Decide type
            type_ = "Income" if diff > 0 else "Expense"
            adjustment_amount = abs(diff)
            note = "Balance adjustment"

            date_str = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M:%S")
            transactions_sheet.append_row([date_str, type_, adjustment_amount, "adjustment", place, note])

            return reply_text(event.reply_token,
                              f"✅ Balance for {place.capitalize()} adjusted by {adjustment_amount} TWD "
                              f"({type_}). New balance: {target_balance} TWD")
        else:
            return reply_text(event.reply_token, "Usage: setbalance place amount\nExample: setbalance cash 2000")

    # Monthly report
    elif text.lower().startswith("report"):
        if len(parts) == 2:
            try:
                year, month = map(int, parts[1].split("-"))
            except Exception:
                today = datetime.today()
                year, month = today.year, today.month
        else:
            today = datetime.today()
            year, month = today.year, today.month
        return reply_text(event.reply_token, get_monthly_report(year, month))

    # Set budget
    elif text.lower().startswith("setbudget"):
        if len(parts) == 3:
            category, amount = parts[1], int(parts[2])
            set_budget(category, amount)
            return reply_text(event.reply_token, f"✅ Budget set for {category}: {amount} TWD")

    # View budget
    elif text.lower() == "budget":
        today = datetime.today()
        return reply_text(event.reply_token, get_budget_status(today.year, today.month))

    # Help
    reply = "Format: e/i amount category place note(optional)\nex: e 500 food cash lunch | i 10000 salary cathay"
    return reply_text(event.reply_token, reply)

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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
