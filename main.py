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
reports_sheet = spreadsheet.worksheet("Reports")

# LINE
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# ===== SHEET FUNCTIONS =====
def add_transaction(type_, amount, category, place, note=""):
    date_value = datetime.now(TIMEZONE)
    date_text = date_value.strftime("%m/%d/%Y %H:%M:%S")
    transactions_sheet.append_row([date_text, type_, amount, category, place, note], value_input_option="USER_ENTERED")
    print(f"[{datetime.now().isoformat()}] Appended transaction -> {date_text} | {type_} | {amount} | {category} | {place}")
    return f"✅ NT${amount:,} {type_} ({category}) {'to' if type_=='Income' else 'from'} {place} saved."

def add_transfer(from_place, to_place, amount, note=""):
    date_value = datetime.now(TIMEZONE)
    date_text = date_value.strftime("%m/%d/%Y %H:%M:%S")
    transfers_sheet.append_row([date_text, from_place, to_place, amount, note], value_input_option="USER_ENTERED")
    print(f"[{datetime.now().isoformat()}] Appended transaction -> {date_text} | Transfer | {amount} |  | {from_place} | {to_place}")
    return f"🔄 Transfer {amount} TWD from {from_place} to {to_place} saved."

def set_balance(place, amount):
    values = balances_sheet.get_all_values()
    places = [row[0].lower() for row in values[1:]]  # skip header
    if place.lower() in places:
        row_idx = places.index(place.lower()) + 2
        balances_sheet.update_cell(row_idx, 2, amount)  # column B = Initial
    else:
        balances_sheet.append_row([place.capitalize(), amount])
    return f"✅ Initial balance for {place.capitalize()} set: {amount} TWD"

def get_balance_report():
    values = balances_sheet.get_all_values()
    rows = values[1:]
    report = "📊 Current Balances:\n"
    for row in rows:
        place, initial, balance, net = row
        report += f"- {place}: In {initial}, Bal {balance}, Net {net}\n"
    return report

def get_report(year, month):
    values = reports_sheet.get_all_values()
    header = values[0]
    rows = values[1:]
    report = f"📅 Report for {year}-{month:02d}\n"
    for row in rows:
        report += " | ".join(row) + "\n"
    return report

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
            return reply_text(event.reply_token, "❌ Format: e/i amount category place note(optional)")
        category = (parts[2] if len(parts) > 2 else "Other").capitalize()
        place = (parts[3] if len(parts) > 3 else "Unknown").capitalize()
        note = " ".join(parts[4:]) if len(parts) > 4 else ""
        return reply_text(event.reply_token, add_transaction(type_, amount, category, place, note))

    # ---- Transfer ----
    elif cmd == "transfer" and len(parts) >= 4:
        from_place, to_place = parts[1].capitalize(), parts[2].capitalize()
        amount = int(parts[3])
        note = " ".join(parts[4:]) if len(parts) > 4 else ""
        return reply_text(event.reply_token, add_transfer(from_place, to_place, amount, note))

    # ---- Balance ----
    elif cmd == "balance":
        return reply_text(event.reply_token, get_balance_report())
    elif cmd == "setbalance" and len(parts) == 3:
        place = parts[1].capitalize()
        amount = int(parts[2])
        return reply_text(event.reply_token, set_balance(place, amount))

    # ---- Report ----
    elif cmd == "report":
        today = datetime.now(TIMEZONE)
        if len(parts) == 2 and "-" in parts[1]:
            try:
                year, month = map(int, parts[1].split("-"))
            except:
                year, month = today.year, today.month
        else:
            year, month = today.year, today.month
        return reply_text(event.reply_token, get_report(year, month))

    # ---- Help ----
    elif cmd == "help":
        help_text = (
            "🤖 Finance Bot Commands:\n\n"
            "📌 Transactions:\n"
            "- i <amount> <category> <place> [note]\n"
            "- e <amount> <category> <place> [note]\n\n"
            "📌 Transfers:\n"
            "- transfer <from> <to> <amount> [note]\n\n"
            "📌 Balances:\n"
            "- balance\n"
            "- setbalance <place> <amount>\n\n"
            "📌 Reports:\n"
            "- report <year>-<month>\n\n"
            "📌 Other:\n"
            "- help"
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
