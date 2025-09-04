import os
import json
from datetime import datetime, timedelta
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

# Ensure Transfers sheet exists
try:
    transfers_sheet = spreadsheet.worksheet("Transfers")
except gspread.exceptions.WorksheetNotFound:
    transfers_sheet = spreadsheet.add_worksheet(title="Transfers", rows="100", cols="4")
    transfers_sheet.update("A1:D1", [["Date", "Amount", "From", "To"]])

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
    # Only calculate balances based on transactions, ignore sheet values
    records = transactions_sheet.get_all_records()
    balances = {}
    for row in records:
        amount = row["Amount"]
        place = row["Place"].capitalize()
        if place not in balances:
            balances[place] = 0
        if row["Type"].lower().startswith("i"):
            balances[place] += amount
        elif row["Type"].lower().startswith("e"):
            balances[place] -= amount
    # Add places from balances_sheet if not present in transactions
    balances_records = balances_sheet.get_all_values()[1:]
    for row in balances_records:
        place = row[0].capitalize()
        if place not in balances:
            balances[place] = int(row[1])
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

# Help
def get_help_text():
    return (
        "🤖 Finance Bot Commands:\n\n"
        "📌 Record Transactions:\n"
        "- i <amount> <category> <place> [note]\n"
        "- e <amount> <category> <place> [note]\n"
        "   ex: e 500 food cash lunch\n"
        "   ex: i 10000 salary cathay\n\n"
        "📌 Transfer:\n"
        "- transfer <amount> <from_place> <to_place> [note] → move money between balances\n\n"
        "📌 Balance:\n"
        "- balance → show all balances\n"
        "- setbalance <place> <amount> → set initial balance for a place\n\n"
        "📌 Reports:\n"
        "- report <year>-<month> → monthly report by category\n\n"
        "📌 Budget:\n"
        "- setbudget <category> <amount>\n"
        "- budget → show budgets\n\n"
        "📌 Data Management:\n"
        "- delete → remove last transaction\n"
        "- reset daily → reset today’s transactions\n"
        "- reset weekly → reset this week’s transactions\n"
        "- reset monthly → reset this month’s transactions\n"
        "- refresh → recalc balances & categories (budgets kept, setbalance preserved)\n\n"
        "📌 Help:\n"
        "- help → show this message\n\n"
        "📌 Search:\n"
        "- search <keyword> [YYYY-MM or YYYY-MM-DD] → find transactions"
    )

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
    
    # ---- Transfer ----
    elif parts[0].lower() == "transfer" and len(parts) >= 4:
        try:
            amount = int(parts[1])
        except ValueError:
            return reply_text(event.reply_token, "❌ Invalid amount. Example: transfer 500 cash post note(optional)")

        from_place = parts[2].capitalize()
        to_place = parts[3].capitalize()
        note = " ".join(parts[4:]) if len(parts) > 4 else ""

        # Get current balances
        balances_records = balances_sheet.get_all_values()[1:]
        balances = {row[0].capitalize(): int(row[1]) for row in balances_records}
        if from_place not in balances:
            return reply_text(event.reply_token, f"❌ {from_place} does not exist in balances.")
        if balances[from_place] < amount:
            return reply_text(event.reply_token, f"❌ Not enough funds in {from_place}. Available: {balances[from_place]} TWD")

        # Update balances directly
        balances[from_place] -= amount
        balances[to_place] = balances.get(to_place, 0) + amount

        balances_sheet.clear()
        balances_sheet.update("A1:B1", [["Place", "Balance"]])
        rows = [[p, b] for p, b in balances.items()]
        balances_sheet.update("A2", rows)

        # Record transfer in Transfers sheet
        date_str = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
        transfers_sheet.append_row([date_str, amount, from_place, to_place])

        reply = f"🔄 Transferred {amount} TWD from {from_place} to {to_place}."
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
    
    elif text.lower() == "delete last":
        all_rows = transactions_sheet.get_all_values()
        if len(all_rows) <= 1:
            return reply_text(event.reply_token, "⚠️ No transactions to delete.")

        last_row = all_rows[-1]
        transactions_sheet.delete_rows(len(all_rows))

        # old parsing (wrong order)
        # date, category, amount, type_, place = last_row[:5]

        # Parse last transaction
        _, type_, amount, category, place, *_ = last_row
        amount = int(amount)

        # Rollback balances
        balances = get_balance()  # Get current balance
        if type_.lower().startswith("i"):
            update_balances(place, -amount, "Income")
        elif type_.lower().startswith("e"):
            update_balances(place, amount, "Expense")

        # Rollback categories
        cat_data = get_categories()
        cat_key = category.lower()
        if cat_key in cat_data:
            if type_.lower().startswith("i"):
                cat_data[cat_key]["total"] -= amount
            else:
                cat_data[cat_key]["total"] += amount
            cat_sheet = spreadsheet.worksheet("Categories")
            cat_sheet.clear()
            cat_sheet.update("A1:C1", [["Category", "Total", "Budget"]])
            rows = [[cat.capitalize(), data["total"], data["budget"]] for cat, data in cat_data.items()]
            cat_sheet.update("A2", rows)

        return reply_text(event.reply_token, f"🗑️ Deleted last transaction: {type_} {amount} {category} at {place}")

    # Reset transactions (daily/weekly/monthly)
    elif parts[0].lower() == "reset" and len(parts) > 1:
        period = parts[1].lower()
        transactions = transactions_sheet.get_all_records()

        today = datetime.now(TIMEZONE)
        
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

        for i in reversed(rows_to_delete):
            transactions_sheet.delete_rows(i)
        return reply_text(event.reply_token, f"♻️ Reset {period} transactions: {len(rows_to_delete)} deleted.")

    # Refresh data
    elif text.lower() == "refresh":
        # Get existing balances first (so setbalance values are preserved)
        balances_records = balances_sheet.get_all_values()[1:]
        balances = {row[0].capitalize(): int(row[1]) for row in balances_records}

        records = transactions_sheet.get_all_records()
        categories = {}

        for row in records:
            amount = row["Amount"]
            place = row["Place"].capitalize()
            category = row["Category"].lower()
            type_ = row["Type"].lower()

            # Balances
            if place not in balances:
                balances[place] = 0
            if type_.startswith("i"):
                balances[place] += amount
            elif type_.startswith("e"):
                balances[place] -= amount

            # Categories (only totals recalculated, budget preserved later)
            if category not in categories:
                categories[category] = {"total": 0}
            if type_.startswith("i"):
                categories[category]["total"] += amount
            elif type_.startswith("e"):
                categories[category]["total"] -= amount

        # --- Update Balances sheet ---
        balances_sheet.clear()
        balances_sheet.update("A1:B1", [["Place", "Balance"]])
        rows = [[p, b] for p, b in balances.items()]
        balances_sheet.update("A2", rows)

        # --- Update Categories sheet, keep budgets ---
        cat_data = get_categories()
        for cat, data in categories.items():
            if cat in cat_data:
                cat_data[cat]["total"] = data["total"]  # update only total
            else:
                cat_data[cat] = {"total": data["total"], "budget": 0}  # new cat

        cat_sheet = spreadsheet.worksheet("Categories")
        cat_sheet.clear()
        cat_sheet.update("A1:C1", [["Category", "Total", "Budget"]])
        rows = [[cat.capitalize(), data["total"], data["budget"]] for cat, data in cat_data.items()]
        cat_sheet.update("A2", rows)

        return reply_text(event.reply_token, "🔄 Data refreshed! (Budgets kept, SetBalance preserved)")

    # ---- Help ----
    elif parts[0].lower() == "help":
        return reply_text(event.reply_token, get_help_text())
    
    # ---- Search transactions ----
    elif parts[0].lower() == "search" and len(parts) >= 2:
        keyword = parts[1].lower()
        date_filter = None
        if len(parts) >= 3:
            date_filter = parts[2]  # format: YYYY-MM or YYYY-MM-DD

        records = transactions_sheet.get_all_records()
        results = []
        for row in records:
            row_date = datetime.strptime(row["Date"], "%Y-%m-%d %H:%M:%S")
            match_keyword = (
                keyword in str(row["Category"]).lower() or
                keyword in str(row["Place"]).lower() or
                keyword in str(row["Note"]).lower()
            )

            match_date = True
            if date_filter:
                if len(date_filter) == 7:  # YYYY-MM
                    match_date = row_date.strftime("%Y-%m") == date_filter
                elif len(date_filter) == 10:  # YYYY-MM-DD
                    match_date = row_date.strftime("%Y-%m-%d") == date_filter

            if match_keyword and match_date:
                results.append(f"{row['Date']} | {row['Type']} {row['Amount']} | {row['Category']} | {row['Place']} | {row['Note']}")

        if not results:
            return reply_text(event.reply_token, f"🔍 No transactions found for '{keyword}'{f' on {date_filter}' if date_filter else ''}.")
        
        # Limit output to avoid LINE message length issues
        max_results = 10
        reply_msg = "🔍 Search Results:\n" + "\n".join(results[:max_results])
        if len(results) > max_results:
            reply_msg += f"\n...and {len(results) - max_results} more."
        return reply_text(event.reply_token, reply_msg)

    # ---- Default ----
    return reply_text(event.reply_token, get_help_text())

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