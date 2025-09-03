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
