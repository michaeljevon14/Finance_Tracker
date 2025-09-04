import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo

TIMEZONE = ZoneInfo("Asia/Taipei")

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

# Ensure Balances sheet exists
try:
    balances_sheet = spreadsheet.worksheet("Balances")
except gspread.exceptions.WorksheetNotFound:
    balances_sheet = spreadsheet.add_worksheet(title="Balances", rows="10", cols="2")
    balances_sheet.update("A1:B1", [["Place", "Balance"]])
    for p in ["Cash", "Post", "Cathay"]:
        balances_sheet.append_row([p, 0])

# Ensure Categories sheet exists
try:
    categories_sheet = spreadsheet.worksheet("Categories")
except gspread.exceptions.WorksheetNotFound:
    categories_sheet = spreadsheet.add_worksheet(title="Categories", rows="100", cols="3")
    categories_sheet.update("A1:C1", [["Category", "Total", "Budget"]])

if categories_sheet.col_count < 3:
    categories_sheet.add_cols(1)  # add budget column
    categories_sheet.update("C1", "Budget")

# LINE API
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# ===== Categories (combined with budgets) =====
def get_categories():
    try:
        cat_sheet = spreadsheet.worksheet("Categories")
    except gspread.exceptions.WorksheetNotFound:
        cat_sheet = spreadsheet.add_worksheet(title="Categories", rows="100", cols="3")
        cat_sheet.update("A1:C1", [["Category", "Total", "Budget"]])
    records = cat_sheet.get_all_records()
    # Dict: category -> {"total": total_amount, "budget": budget_amount}
    return {row["Category"].lower(): {"total": row["Total"], "budget": row["Budget"]} for row in records}

def set_budget(category, amount):
    cat_data = get_categories()
    cat_key = category.strip().lower()
    if cat_key in cat_data:
        cat_data[cat_key]["budget"] = amount
    else:
        cat_data[cat_key] = {"total": 0, "budget": amount}

    # Rewrite sheet
    cat_sheet = spreadsheet.worksheet("Categories")
    cat_sheet.clear()
    cat_sheet.update("A1:C1", [["Category", "Total", "Budget"]])
    rows = [[cat.capitalize(), data["total"], data["budget"]] for cat, data in cat_data.items()]
    cat_sheet.update("A2", rows)

def get_budget_status(year, month):
    cat_data = get_categories()
    report = f"🎯 Budget Status ({year}-{month:02d})\n"
    for cat, data in cat_data.items():
        used = abs(data["total"])  # always use absolute value for expenses
        limit = data["budget"]
        pct = int((used / limit) * 100) if limit > 0 else 0
        report += f"- {cat.capitalize()}: {used} / {limit} TWD ({pct}%)\n"
    return report

# ===== Balances =====
def get_balance():
    records = transactions_sheet.get_all_records()
    balances_records = balances_sheet.get_all_values()[1:]
    balances = {row[0].capitalize(): int(row[1]) for row in balances_records}

    # Apply transactions dynamically
    for row in records:
        amount = row["Amount"]
        place = row["Place"].capitalize()
        if place not in balances:
            balances[place] = 0
        if row["Type"].lower().startswith("i"):
            balances[place] += amount
        elif row["Type"].lower().startswith("e"):
            balances[place] -= amount
    return balances

def update_balances(place, amount, type_):
    balances_records = balances_sheet.get_all_values()[1:]
    balances = {row[0].capitalize(): int(row[1]) for row in balances_records}
    place = place.capitalize()
    if place not in balances:
        balances[place] = 0
    if type_.lower().startswith("i"):
        balances[place] += amount
    else:
        balances[place] -= amount

    # Rewrite sheet
    balances_sheet.clear()
    balances_sheet.update("A1:B1", [["Place", "Balance"]])
    rows = [[p, b] for p, b in balances.items()]
    balances_sheet.update("A2", rows)

def format_balance_report(balances):
    report = "📊 Current Balances:\n"
    total = 0
    for place, amt in balances.items():
        report += f"- {place}: {amt} TWD\n"
        total += amt
    report += f"💰 Total: {total} TWD"
    return report

# ===== Monthly report =====
def get_monthly_report(year, month):
    records = transactions_sheet.get_all_records()
    total_income, total_expense = 0, 0
    categories = get_categories()
    income_by_cat = {cat: 0 for cat in categories}
    expense_by_cat = {cat: 0 for cat in categories}

    for row in records:
        row_date = datetime.strptime(row["Date"], "%Y-%m-%d %H:%M:%S")
        if row_date.year == year and row_date.month == month:
            amount = row["Amount"]
            category = row["Category"].lower()
            if row["Type"].lower().startswith("i"):
                total_income += amount
                income_by_cat[category] = income_by_cat.get(category, 0) + amount
            elif row["Type"].lower().startswith("e"):
                total_expense += amount
                expense_by_cat[category] = expense_by_cat.get(category, 0) + amount

    net = total_income - total_expense
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

# ===== LINE webhook =====
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

    # ---- Record transaction ----
    if parts[0].lower() in ("i", "income", "e", "expense"):
        type_ = "Income" if parts[0].lower() in ("i", "income") else "Expense"
        try:
            amount = int(parts[1])
        except Exception:
            return reply_text(event.reply_token,
                "Format: e/i amount category place note(optional)\nex: e 500 food cash lunch")

        category = parts[2] if len(parts) > 2 else "Other"
        place = parts[3] if len(parts) > 3 else "Unknown"
        note = " ".join(parts[4:]) if len(parts) > 4 else ""

        date_str = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
        transactions_sheet.append_row([date_str, type_, amount, category, place, note])
        update_balances(place, amount, type_)

        # Update Categories sheet whenever a transaction is added
        cat_data = get_categories()
        cat_key = category.lower()
        if cat_key in cat_data:
            # Update total
            if type_ == "Income":
                cat_data[cat_key]["total"] += amount
            else:
                cat_data[cat_key]["total"] = int(cat_data[cat_key]["total"]) - amount
        else:
            # Add new category with initial total
            cat_data[cat_key] = {"total": amount, "budget": 0}

        # Rewrite sheet
        cat_sheet = spreadsheet.worksheet("Categories")
        cat_sheet.clear()
        cat_sheet.update("A1:C1", [["Category", "Total", "Budget"]])
        rows = [[cat.capitalize(), data["total"], data["budget"]] for cat, data in cat_data.items()]
        cat_sheet.update("A2", rows)

        reply = f"✅ NT${amount:,} {type_} ({category}) {'to' if type_=='Income' else 'from'} {place} saved."
        return reply_text(event.reply_token, reply)

    # ---- Balance ----
    elif text.lower() == "balance":
        balances = get_balance()
        return reply_text(event.reply_token, format_balance_report(balances))

    # ---- Set initial balance ----
    elif parts[0].lower() == "setbalance" and len(parts) == 3:
        place = parts[1].capitalize()
        try:
            amount = int(parts[2])
        except ValueError:
            return reply_text(event.reply_token, "❌ Invalid amount. Example: setbalance cash 2000")

        balances_records = balances_sheet.get_all_values()[1:]
        balances = {row[0].capitalize(): int(row[1]) for row in balances_records}
        balances[place] = amount
        balances_sheet.clear()
        balances_sheet.update("A1:B1", [["Place", "Balance"]])
        rows = [[p, b] for p, b in balances.items()]
        balances_sheet.update("A2", rows)

        return reply_text(event.reply_token, f"✅ Balance for {place} set: {amount} TWD")

    # ---- Monthly report ----
    elif text.lower().startswith("report"):
        if len(parts) == 2:
            try:
                year, month = map(int, parts[1].split("-"))
            except Exception:
                today = datetime.now(TIMEZONE)
                year, month = today.year, today.month
        else:
            today = datetime.now(TIMEZONE)
            year, month = today.year, today.month
        return reply_text(event.reply_token, get_monthly_report(year, month))

    # ---- Set budget ----
    elif text.lower().startswith("setbudget") and len(parts) == 3:
        category = parts[1]
        try:
            amount = int(parts[2])
        except ValueError:
            return reply_text(event.reply_token, "❌ Invalid amount. Example: setbudget food 5000")
        set_budget(category, amount)
        return reply_text(event.reply_token, f"✅ Budget set for {category}: {amount} TWD")

    # ---- Show budget ----
    elif text.lower() == "budget":
        today = datetime.now(TIMEZONE)
        return reply_text(event.reply_token, get_budget_status(today.year, today.month))
    
    # Delete last transaction
    elif parts[0].lower() == "delete" and len(parts) > 1 and parts[1].lower() == "last":
        transactions = transactions_sheet.get_all_values()
        if len(transactions) <= 1:
            reply_text(event.reply_token, "❌ No transactions to delete.")
            return
        
        last_row = transactions[-1]
        date, category, amount, ttype, place = last_row
        amount = float(amount)

        # Rollback balance
        balances = balances_sheet.get_all_records()
        for i, b in enumerate(balances, start=2):
            if b["Place"].lower() == place.lower():
                if ttype.lower() == "income":
                    balances_sheet.update_cell(i, 2, b["Balance"] - amount)
                else:  # expense
                    balances_sheet.update_cell(i, 2, b["Balance"] + amount)

        # Rollback category
        categories = categories_sheet.get_all_records()
        for i, c in enumerate(categories, start=2):
            if c["Category"].lower() == category.lower():
                categories_sheet.update_cell(i, 2, c["Total"] - amount)

        # Delete last row
        transactions_sheet.delete_rows(len(transactions))

        reply_text(event.reply_token, f"✅ Deleted last transaction: {category} {amount} ({ttype}).")

    # Reset transactions (daily/weekly/monthly)
    elif parts[0].lower() == "reset" and len(parts) > 1:
        period = parts[1].lower()
        transactions = transactions_sheet.get_all_records()

        from datetime import datetime, timedelta
        today = datetime.today()
        
        if period == "daily":
            cutoff = today.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "weekly":
            cutoff = today - timedelta(days=today.weekday())  # start of week
        elif period == "monthly":
            cutoff = today.replace(day=1)
        else:
            reply_text(event.reply_token, "❌ Unknown reset period. Use daily, weekly, or monthly.")
            return

        rows_to_delete = []
        for i, t in enumerate(transactions, start=2):  # row 2 = first transaction
            t_date = datetime.strptime(t["Date"], "%Y-%m-%d %H:%M:%S")
            if t_date >= cutoff:
                rows_to_delete.append(i)

        if not rows_to_delete:
            reply_text(event.reply_token, f"ℹ️ No {period} transactions to reset.")
            return

        for row in reversed(rows_to_delete):  # delete from bottom
            transactions_sheet.delete_rows(row)

        # After reset, refresh data
        # (balances & categories auto-fixed)
        # You can call refresh here:
        # refresh_data()

        reply_text(event.reply_token, f"🗑️ {period.capitalize()} transactions reset.")

    # Refresh data
    elif parts[0].lower() == "refresh":
        balances_sheet.clear()
        balances_sheet.update("A1:B1", [["Place", "Balance"]])
        
        categories_sheet.clear()
        categories_sheet.update("A1:C1", [["Category", "Total", "Budget"]])

        transactions = transactions_sheet.get_all_records()
        balances = {}
        categories = {}
        for t in transactions:
            place = t["Place"]
            category = t["Category"]
            amount = float(t["Amount"])
            ttype = t["Type"]

            if place not in balances:
                balances[place] = 0
            if category not in categories:
                categories[category] = 0

            if ttype.lower() == "income":
                balances[place] += amount
                categories[category] += amount
            else:
                balances[place] -= amount
                categories[category] -= amount

        for place, bal in balances.items():
            balances_sheet.append_row([place, bal])

        for cat, total in categories.items():
            categories_sheet.append_row([cat, total, ""])

        reply_text(event.reply_token, "🔄 Data refreshed successfully.")

    # ---- Help ----
    elif parts[0].lower() == "help":
        help_text = (
            "🤖 Finance Bot Commands:\n\n"
            "📌 Record Transactions:\n"
            "- i <amount> <category> <place> [note]\n"
            "- e <amount> <category> <place> [note]\n\n"
            "📌 Balance:\n"
            "- balance → show all balances\n"
            "- setbalance <place> <amount> → set initial balance for a place\n\n"
            "📌 Reports:\n"
            "- report <year>-<month> → monthly report by category\n\n"
            "📌 Budget:\n"
            "- setbudget <category> <amount>\n"
            "- budget → show budgets\n\n"
            "📌 Help:\n"
            "- help → show this message"
        )
        return reply_text(event.reply_token, help_text)

    # ---- Default ----
    else:
        reply = (
            "Available commands:\n"
            "e/i amount category place note(optional)\n"
            "balance | report YYYY-MM | setbudget category amount | budget\n"
            "setbalance place amount | delete last | reset daily/weekly/monthly | refresh"
        )
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
