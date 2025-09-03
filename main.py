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

# ----- ENV VARS (must match names you set on Render) -----
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
    raise RuntimeError("Missing environment variables: " + ", ".join(missing) +
                       ". Set them in Render: Service → Environment → Add Environment Variable.")

# Parse Google credentials JSON (must be the full JSON string)
try:
    creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
except Exception as e:
    raise RuntimeError("GOOGLE_CREDENTIALS_JSON is not valid JSON: " + str(e))

scopes = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_info(creds_info, scopes=scopes)

# Authorize gspread with credentials
gc = gspread.authorize(creds)
sheet = gc.open(SHEET_NAME).sheet1  # first worksheet

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# ===== Balance Report =====
def get_balance(sheet):
    records = sheet.get_all_records()
    balances = {"cash": 0, "post": 0, "cathay": 0}
    for row in records:
        amount = row["Amount"]
        place = row["Place"].lower()
        if place in balances:
            if row["Type"].lower().startswith("i"):  # income
                balances[place] += amount
            else:  # expense
                balances[place] -= amount
    return balances

def format_balance_report(balances):
    return (
        "📊 Current Balances:\n"
        f"- Cash: {balances['cash']} TWD\n"
        f"- Post Bank: {balances['post']} TWD\n"
        f"- Cathay Bank: {balances['cathay']} TWD\n"
        f"💰 Total: {sum(balances.values())} TWD"
    )


# ===== Monthly Report =====
def get_monthly_report(sheet, year, month):
    records = sheet.get_all_records()
    income, expense = 0, 0
    categories = {}

    for row in records:
        row_date = datetime.strptime(row["Date"], "%Y-%m-%d")
        if row_date.year == year and row_date.month == month:
            amount = row["Amount"]
            category = row["Category"]
            if row["Type"].lower().startswith("i"):
                income += amount
            else:
                expense += amount
                categories[category] = categories.get(category, 0) + amount

    net = income - expense
    top_cats = sorted(categories.items(), key=lambda x: x[1], reverse=True)[:3]

    report = (
        f"📅 Report for {year}-{month:02d}\n"
        f"Income: {income} TWD\n"
        f"Expenses: {expense} TWD\n"
        f"Net Savings: {net} TWD\n\n"
        "Top Categories:\n"
    )
    for cat, amt in top_cats:
        report += f"- {cat}: {amt} TWD\n"
    return report


# ===== Budgeting =====
def get_budgets(sheet):
    try:
        budget_sheet = sheet.worksheet("Budgets")
    except:
        budget_sheet = sheet.add_worksheet(title="Budgets", rows="100", cols="2")
        budget_sheet.update("A1:B1", [["Category", "Amount"]])
    records = budget_sheet.get_all_records()
    return {row["Category"].lower(): row["Amount"] for row in records}

def set_budget(sheet, category, amount):
    budgets = get_budgets(sheet)
    budgets[category.lower()] = amount
    budget_sheet = sheet.worksheet("Budgets")
    budget_sheet.clear()
    budget_sheet.update("A1:B1", [["Category", "Amount"]])
    rows = [[cat, amt] for cat, amt in budgets.items()]
    budget_sheet.update("A2", rows)

def get_budget_status(sheet, year, month):
    budgets = get_budgets(sheet)
    records = sheet.get_all_records()
    spent = {}

    for row in records:
        row_date = datetime.strptime(row["Date"], "%Y-%m-%d")
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

# LINE webhook (POST)
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

    if parts[0].lower() in ("e", "i"):
        type_ = "Expense" if parts[0].lower() == "e" else "Income"
        try:
            amount = int(parts[1])
        except Exception:
            reply = "Format: e/i amount category place note(optional)\nex: e 500 food cash lunch"
            return reply_text(event.reply_token, reply)

        category = parts[2] if len(parts) > 2 else "Other"
        place = parts[3] if len(parts) > 3 else "Unknown"
        note = " ".join(parts[4:]) if len(parts) > 4 else ""

        # 🗓 Asia/Taipei timestamp
        date_str = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M:%S")

        # Append in correct order: Date | Type | Amount | Category | Place | Note
        sheet.append_row([date_str, type_, amount, category, place, note])

        reply = f"✅ NT${amount:,} {type_} ({category}) {('from' if type_=='Expense' else 'to')} {place} saved."
        return reply_text(event.reply_token, reply)
    
    if text.lower() == "balance":
        balances = get_balance(sheet)
        reply_text_msg = format_balance_report(balances)
        return reply_text(event.reply_token, reply_text_msg)

    elif text.lower().startswith("report"):
        parts = text.split()
        if len(parts) == 2:  # e.g. "report 2025-08"
            year, month = map(int, parts[1].split("-"))
        else:
            today = datetime.today()
            year, month = today.year, today.month
        reply_text_msg = get_monthly_report(sheet, year, month)
        return reply_text(event.reply_token, reply_text_msg)

    elif text.lower().startswith("setbudget"):
        parts = text.split()
        if len(parts) == 3:
            category, amount = parts[1], int(parts[2])
            set_budget(sheet, category, amount)
            reply_text_msg = f"✅ Budget set for {category}: {amount} TWD"
            return reply_text(event.reply_token, reply_text_msg)

    elif text.lower() == "budget":
        today = datetime.today()
        reply_text_msg = get_budget_status(sheet, today.year, today.month)
        return reply_text(event.reply_token, reply_text_msg)

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

if __name__ == "__main__":
    # Local dev only; Render will run via gunicorn
    app.run(host="0.0.0.0", port=5000)
