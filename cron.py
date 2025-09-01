import os, json
from datetime import datetime
import pytz

from linebot import LineBotApi
from linebot.models import TextSendMessage

import gspread
from google.oauth2.service_account import Credentials
 
from main import summarize_month # reuse logic
 
CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
SHEET_ID = os.environ["SHEET_ID"]
OWNER_USER_ID = os.environ.get("OWNER_USER_ID")
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Taipei")

if not OWNER_USER_ID:
    raise RuntimeError("Set OWNER_USER_ID env var with your LINE userId to receive push messages")
line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def get_gspread_client():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)

def prev_month_key(tzname: str)-> str:
    tz = pytz.timezone(tzname)
    now = datetime.now(tz)
    y = now.year
    m = now.month- 1
    if m == 0:
        m = 12
        y-= 1
    return f"{y}-{m:02d}"

def main():
    gc = get_gspread_client()
    sh = gc.open_by_key(SHEET_ID)
    month = prev_month_key(TIMEZONE)
    summary = summarize_month(sh, month)
    line_bot_api.push_message(OWNER_USER_ID, TextSendMessage(text=summary))

if __name__ == "__main__":
    main()