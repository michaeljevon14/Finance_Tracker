import os, json, re
from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta


import pytz
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse


from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage


import gspread
from google.oauth2.service_account import Credentials


# --- Environment ---
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
SHEET_ID = os.environ.get("SHEET_ID")
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Taipei")
OWNER_USER_ID = os.environ.get("OWNER_USER_ID") # optional; can be captured dynamically


if not (CHANNEL_SECRET and CHANNEL_ACCESS_TOKEN and SHEET_ID):
    raise RuntimeError("Missing required env vars: LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN, SHEET_ID")


# --- LINE SDK ---
line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)


# --- Google Sheets client ---
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_gspread_client():
    json_str = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not json_str:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON env var")
    info = json.loads(json_str)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)




def ensure_worksheets(gc):
    sh = gc.open_by_key(SHEET_ID)
    try:
        tx = sh.worksheet("Transactions")
    except gspread.WorksheetNotFound:
        tx = sh.add_worksheet(title="Transactions", rows=1000, cols=10)
        tx.append_row(["Timestamp", "Date", "Type", "Category", "Amount", "Notes", "UserId"]) # headers
    try:
        bd = sh.worksheet("Budgets")
    except gspread.WorksheetNotFound:
        bd = sh.add_worksheet(title="Budgets", rows=100, cols=5)
        bd.append_row(["Month", "Category", "Budget"]) # headers
    return sh




def tz_now():
    tz = pytz.timezone(TIMEZONE)
    return datetime.now(tz)




# --- Business logic ---
EXPENSE_RE = re.compile(r"^\s*\+\s*(\d+(?:\.\d+)?)\s+([\w-]+)(?:\s+(.*))?\s*$", re.I)
INCOME_RE1 = re.compile(r"^\s*\+i\s+(\d+(?:\.\d+)?)\s+([\w-]+)(?:\s+(.*))?\s*$", re.I)
INCOME_RE2 = re.compile(r"^\s*income\s+(\d+(?:\.\d+)?)\s+([\w-]+)(?:\s+(.*))?\s*$", re.I)
BUDGET_RE = re.compile(r"^\s*budget\s+([\w-]+)\s+(\d+(?:\.\d+)?)\s*$", re.I)
SUMMARY_RE = re.compile(r"^\s*summary(?:\s+(\d{4})-(\d{2}))?\s*$", re.I)




def append_transaction(sh, ttype: str, category: str, amount: float, notes: str, user_id: str):
    ws = sh.worksheet("Transactions")
    now = tz_now()
    ws.append_row([
        now.isoformat(),
        now.strftime("%Y-%m-%d"),
        ttype,
        category.lower(),
        amount,
        notes or "",
        user_id or "",
    ])

def set_budget(sh, month: str, category: str, amount: float):
    ws = sh.worksheet("Budgets")
    # Try to find existing (month, category)
    data = ws.get_all_records()
    for i, row in enumerate(data, start=2): # header is row 1
        if row.get("Month") == month and row.get("Category", "").lower() == category.lower():
            ws.update_cell(i, 3, amount) # column 3 = Budget
            return "Updated budget for %s in %s to %.0f" % (category, month, amount)
    # Not found → append
    ws.append_row([month, category.lower(), amount])
    return "Set budget for %s in %s to %.0f" % (category, month, amount)

def month_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m")

def summarize_month(sh, month: str):
    tx = sh.worksheet("Transactions").get_all_records()
    total_income = 0.0
    total_expense = 0.0
    by_cat = {}
    for row in tx:
        date = row.get("Date")
        if not date or not isinstance(date, str):
            continue
        if not date.startswith(month):
            continue
        ttype = (row.get("Type") or "").lower()
        cat = (row.get("Category") or "uncategorized").lower()
        amt = float(row.get("Amount") or 0)
        if ttype == "income":
            total_income += amt
        elif ttype == "expense":
            total_expense += amt
            by_cat[cat] = by_cat.get(cat, 0.0) + amt
    # Budgets
    budgets_ws = sh.worksheet("Budgets").get_all_records()
    budgets = {}
    for row in budgets_ws:
        if row.get("Month") == month:
            budgets[row.get("Category", "").lower()] = float(row.get("Budget") or 0)

    # Build text summary
    lines = [
        f"📆 {month} Summary",
        f"Income: {int(total_income):,}",
        f"Expenses: {int(total_expense):,}",
        f"Savings: {int(total_income - total_expense):,}",
        "",
        "By Category:",
    ]
    for cat, spent in sorted(by_cat.items(), key=lambda x: -x[1]):
        bud = budgets.get(cat)
        if bud:
            pct = (spent / bud * 100) if bud else 0
            lines.append(f"- {cat}: {int(spent):,} / {int(bud):,} ({pct:.0f}%)")
        else:
            lines.append(f"- {cat}: {int(spent):,}")
    return "\n".join(lines)


# --- FastAPI app ---
app = FastAPI()

@app.get("/")
async def health():
    return {"ok": True}

@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = (await request.body()).decode("utf-8")
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return PlainTextResponse("OK")

@handler.add(MessageEvent, message=TextMessage)
def handle_text(event: MessageEvent):
    user_id = event.source.user_id
    text = (event.message.text or "").strip()

    gc = get_gspread_client()
    sh = ensure_worksheets(gc)

    # Commands
    m = EXPENSE_RE.match(text)
    if m:
        amount = float(m.group(1))
        category = m.group(2)
        notes = m.group(3) or ""
        append_transaction(sh, "expense", category, amount, notes, user_id)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ Logged expense: {int(amount):,} {category} {('- ' + notes) if notes else ''}"))
        return

    for income_re in (INCOME_RE1, INCOME_RE2):
        m = income_re.match(text)
        if m:
            amount = float(m.group(1))
            category = m.group(2)
            notes = m.group(3) or ""
            append_transaction(sh, "income", category, amount, notes, user_id)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"💰 Logged income: {int(amount):,} {category} {('- ' + notes) if notes else ''}"))
            return

    m = BUDGET_RE.match(text)
    if m:
        category = m.group(1)
        amount = float(m.group(2))
        month = text.split()[-1] if re.search(r"\d{4}-\d{2}$", text) else month_key(tz_now())
        msg = set_budget(sh, month, category, amount)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"📝 {msg}"))
        return

    m = SUMMARY_RE.match(text)
    if m:
        year, mon = m.groups()
        month = f"{year}-{mon}" if (year and mon) else month_key(tz_now())
        summary = summarize_month(sh, month)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=summary))
        return
    
    # Help
    help_text = (
        "Commands:\n"
        "+<amount> <category> [notes] - Log expense\n"
        "+i <amount> <category> [notes] - Log income (or: income <amount> <category>)\n"
        "budget <category> <amount> - Set this month's budget for category\n"
        "summary [YYYY-MM] - Show summary (default: current month)"
    )
    line_bot_api