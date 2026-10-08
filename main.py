import os
import re
import time
import random
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from html import escape
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from supabase import create_client, Client

# ------------------------ এনভায়রনমেন্ট ------------------------
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY")
if not all([TELEGRAM_TOKEN, SUPABASE_URL, SUPABASE_KEY]):
    raise ValueError(".env ফাইলে সব ভেরিয়েবল সেট করুন")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
logger.info("Supabase ক্লায়েন্ট রেডি")

# ------------------------ স্ট্যাটাস ------------------------
STATUS_OPTIONS = ["Pending", "Approved", "Processing", "Contact", "Contact Fail", "Fail"]
STATUS_CODES = {
    "Pending": "PEND",
    "Approved": "APPR",
    "Processing": "PROC",
    "Contact": "CONT",
    "Contact Fail": "CFAIL",
    "Fail": "FAIL",
}
REVERSE_STATUS = {v: k for k, v in STATUS_CODES.items()}

def get_status_emoji(status):
    s = (status or "").lower()
    if "contact fail" in s: return "❌"
    if "contact" in s: return "📞"
    if "fail" in s or "reject" in s or "cancel" in s: return "🔴"
    if "approv" in s or "success" in s or "complete" in s or "paid" in s: return "🟢"
    if "pending" in s: return "🟡"
    if "process" in s: return "🔵"
    return "⚪"

# ------------------------ Quick Note টেমপ্লেট ------------------------
NOTE_TEMPLATES = {
    "save": "saving time",
    "call": "call received kore",
    "num":  "last number cacchen",
    "chk":  "check kore janaben",
    "later":"ektu pore call diyen",
}
NOTE_TEMPLATE_LABELS = {
    "save":  "⏱ Saving Time",
    "call":  "📞 Call Received Kore",
    "num":   "🔢 Last Number Cacchen",
    "chk":   "✅ Check Kore Janaben",
    "later": "⏰ Ektu Pore Call Diyen",
}

# ------------------------ New Case ফিল্ড ------------------------
NEW_CASE_FIELDS = [
    ("username",          "Username",           True),
    ("user_password",     "Password",           False),
    ("phone_number",      "Phone Number",       True),
    ("platform",          "Platform",           True),
    ("type",              "Type",               False),
    ("payment_method",    "Payment Method",     False),
    ("withdrawal_speed",  "Withdrawal Speed",   False),
    ("amount",            "Amount",             False),
    ("notes",             "Notes",              False),
]

MAX_RESULTS = 100

# ------------------------ Conversation স্টেট ------------------------
(ASK_EMAIL, ASK_PASSWORD, MAIN_MENU, ASK_NOTE, NEW_CASE_INPUT) = range(5)

# ------------------------ ফাইল হ্যান্ডলিং ------------------------
BLOCKED_FILE = "blocked_users.json"
AUTH_FILE = "authenticated_users.json"
ATTEMPT_FILE = "login_attempts.json"

def _load_json(path, default):
    try:
        with open(path, "r") as f: return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): return default

def _save_json(path, data):
    with open(path, "w") as f: json.dump(data, f)

def is_user_blocked(uid): return str(uid) in set(_load_json(BLOCKED_FILE, []))
def block_user(uid):
    b = set(_load_json(BLOCKED_FILE, [])); b.add(str(uid)); _save_json(BLOCKED_FILE, list(b))
def is_user_authenticated(uid): return str(uid) in set(_load_json(AUTH_FILE, []))
def authenticate_user(uid):
    a = set(_load_json(AUTH_FILE, [])); a.add(str(uid)); _save_json(AUTH_FILE, list(a))
def get_user_attempts(uid): return _load_json(ATTEMPT_FILE, {}).get(str(uid), 0)
def increment_user_attempts(uid):
    a = _load_json(ATTEMPT_FILE, {}); a[str(uid)] = a.get(str(uid), 0) + 1
    _save_json(ATTEMPT_FILE, a)
    if a[str(uid)] >= 3: block_user(uid)
def reset_user_attempts(uid):
    a = _load_json(ATTEMPT_FILE, {})
    if str(uid) in a: del a[str(uid)]; _save_json(ATTEMPT_FILE, a)

# ------------------------ হেল্পার ------------------------
def is_case_id(t): return bool(re.fullmatch(r'W\d{8,}', t, re.IGNORECASE))
def is_phone_number(t):
    c = re.sub(r'[\s\-\(\)]', '', t)
    return bool(re.fullmatch(r'\+?\d{7,15}', c))

def safe(v):
    if v is None or v == "": return "—"
    return escape(str(v))

def clean_for_or(q):
    # supabase .or_() ফিল্টারে , . ( ) " ' ভাঙে — সরিয়ে দিই
    return re.sub(r"[,\.\(\)\"'%\\]", "", q)

def format_datetime(iso_str):
    if not iso_str: return "N/A"
    try:
        s = iso_str[:-1] + '+00:00' if iso_str.endswith('Z') else iso_str
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(ZoneInfo("Asia/Dhaka")).strftime("%d %b %Y, %I:%M %p").lower()
    except Exception:
        return iso_str

def split_datetime(iso_str):
    f = format_datetime(iso_str)
    if "," in f:
        d, t = f.split(",", 1); return d.strip(), t.strip()
    return f, "—"

def gen_case_id():
    # W + ms timestamp + 2 random digits — always W\d{8,} pattern
    return "W" + str(int(time.time() * 100)) + str(random.randint(10, 99))

# ------------------------ Supabase কোয়েরি ------------------------
def search_by_case_id(cid):
    try:
        r = supabase.table("withdrawals").select("*").eq("case_id", cid.upper()).execute()
        return r.data[0] if r.data else None
    except Exception as e:
        logger.error(f"case_id search: {e}"); return None

def search_by_phone(phone):
    try:
        r = supabase.table("withdrawals").select("*").ilike("phone_number", phone)\
            .order("created_at", desc=True).limit(MAX_RESULTS).execute()
        return r.data or []
    except Exception as e:
        logger.error(f"phone search: {e}"); return []

def search_by_username(uname):
    try:
        r = supabase.table("withdrawals").select("*").ilike("username", uname)\
            .order("created_at", desc=True).limit(MAX_RESULTS).execute()
        return r.data or []
    except Exception as e:
        logger.error(f"username search: {e}"); return []

def master_search(q):
    """যেকোনো ফিল্ডে ম্যাচ করলে রেজাল্ট দেখায় (name/number/password/notes ইত্যাদি)"""
    try:
        qc = clean_for_or(q)
        if not qc:
            return []
        or_str = ",".join([
            f"case_id.ilike.%{qc}%",
            f"username.ilike.%{qc}%",
            f"user_password.ilike.%{qc}%",
            f"phone_number.ilike.%{qc}%",
            f"platform.ilike.%{qc}%",
            f"payment_method.ilike.%{qc}%",
            f"type.ilike.%{qc}%",
            f"withdrawal_speed.ilike.%{qc}%",
            f"notes.ilike.%{qc}%",
        ])
        r = supabase.table("withdrawals").select("*").or_(or_str)\
            .order("created_at", desc=True).limit(MAX_RESULTS).execute()
        return r.data or []
    except Exception as e:
        logger.error(f"master search: {e}"); return []

def search_last_7_days():
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        r = supabase.table("withdrawals").select("*").gte("created_at", since)\
            .order("created_at", desc=True).limit(MAX_RESULTS).execute()
        return r.data or []
    except Exception as e:
        logger.error(f"7-day report: {e}"); return []

def search_by_status(status):
    try:
        r = supabase.table("withdrawals").select("*").eq("status", status)\
            .order("created_at", desc=True).limit(MAX_RESULTS).execute()
        return r.data or []
    except Exception as e:
        logger.error(f"status filter: {e}"); return []

def search_all_cases():
    try:
        r = supabase.table("withdrawals").select("*")\
            .order("created_at", desc=True).limit(MAX_RESULTS).execute()
        return r.data or []
    except Exception as e:
        logger.error(f"all cases: {e}"); return []

def update_case_status(case_id, new_status, updated_by):
    try:
        case = search_by_case_id(case_id)
        if not case: return False
        logs = case.get('status_logs') or []
        if not isinstance(logs, list): logs = []
        logs.append({
            "from": case.get('status'),
            "to": new_status,
            "by": updated_by,
            "at": datetime.now(timezone.utc).isoformat(),
        })
        r = supabase.table("withdrawals").update({
            "status": new_status, "status_logs": logs
        }).eq("case_id", case_id).execute()
        return bool(r.data)
    except Exception as e:
        logger.error(f"status update: {e}"); return False

def append_note(case_id, note):
    try:
        case = search_by_case_id(case_id)
        if not case: return False
        existing = case.get('notes') or ""
        ts = datetime.now(ZoneInfo("Asia/Dhaka")).strftime("%d %b %Y, %I:%M %p").lower()
        new = f"[{ts}] {note}"
        combined = (existing + "\n" + new).strip() if existing else new
        r = supabase.table("withdrawals").update({"notes": combined})\
            .eq("case_id", case_id).execute()
        return bool(r.data)
    except Exception as e:
        logger.error(f"append note: {e}"); return False

def create_new_case(data: dict):
    try:
        payload = {
            "case_id": gen_case_id(),
            "status": "Pending",
            **{k: (v if v not in (None, "", "—") else None) for k, v in data.items()},
        }
        # amount numeric কনভার্ট
        if payload.get("amount"):
            try:
                payload["amount"] = float(str(payload["amount"]).replace(",", ""))
            except (ValueError, TypeError):
                payload["amount"] = None
        r = supabase.table("withdrawals").insert(payload).execute()
        return r.data[0] if r.data else None
    except Exception as e:
        logger.error(f"create case: {e}"); return None

# ------------------------ ফরম্যাট ------------------------
def format_single_case(case, index=None, total=None):
    date_str, time_str = split_datetime(case.get('created_at', ''))
    status = case.get('status', 'Pending')
    status_emoji = get_status_emoji(status)

    if index is not None and total and total > 1:
        title = f"║  🗂️  <b>CASE {index} / {total}</b>"
    else:
        title = "║  🗂️  <b>CASE DETAILS</b>"

    lines = [
        "╔═══════════════════════════════╗",
        title,
        "╚═══════════════════════════════╝",
        "",
        "┏━━━ 👤 <b>ACCOUNT</b> ━━━┓",
        f"   🔑 <b>Username</b>  :  <code>{safe(case.get('username'))}</code>",
        f"   🔒 <b>Password</b>  :  <code>{safe(case.get('user_password'))}</code>",
        f"   🆔 <b>Case ID</b>    :  <code>{safe(case.get('case_id'))}</code>",
        f"   📞 <b>Phone</b>       :  <code>{safe(case.get('phone_number'))}</code>",
        "┗━━━━━━━━━━━━━━━━━━━━━━━┛",
        "",
        "┏━━━ 💰 <b>TRANSACTION</b> ━━━┓",
        f"   📱 <b>Platform</b>  :  {safe(case.get('platform'))}",
        f"   📦 <b>Type</b>         :  {safe(case.get('type'))}",
        f"   💳 <b>Payment</b>   :  {safe(case.get('payment_method'))}",
        f"   ⚡ <b>Speed</b>      :  {safe(case.get('withdrawal_speed'))}",
        f"   💵 <b>Amount</b>     :  <b>{safe(case.get('amount'))}</b>",
        "┗━━━━━━━━━━━━━━━━━━━━━━━┛",
        "",
        "┏━━━ 📌 <b>STATUS</b> ━━━┓",
        f"   {status_emoji}  <b>{safe(status)}</b>",
        "┗━━━━━━━━━━━━━━━━━━━━━━━┛",
        "",
        "┏━━━ 📝 <b>NOTES</b> ━━━┓",
    ]
    notes = case.get('notes') or "—"
    if len(str(notes)) > 500: notes = str(notes)[:500] + "…"
    for line in str(notes).split("\n"):
        lines.append(f"   <i>{escape(line)}</i>")
    lines += [
        "┗━━━━━━━━━━━━━━━━━━━━━━━┛",
        "",
        "┏━━━ 🕒 <b>DATE &amp; TIME</b> ━━━┓",
        f"   📅 <b>Date</b>  :  <code>{date_str}</code>",
        f"   ⏰ <b>Time</b>  :  <code>{time_str}</code>",
        "┗━━━━━━━━━━━━━━━━━━━━━━━┛",
        "",
        "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬",
    ]
    return "\n".join(lines)

# ------------------------ কীবোর্ড ------------------------
def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Last 7 Days Report", callback_data="menu:7d")],
        [InlineKeyboardButton("🔍 Search Case", callback_data="menu:search")],
        [InlineKeyboardButton("🗂 Filter by Status", callback_data="menu:filter")],
        [InlineKeyboardButton("➕ New Case", callback_data="menu:newcase")],
        [InlineKeyboardButton("📊 Statistics", callback_data="menu:stats")],
        [InlineKeyboardButton("❓ Help", callback_data="menu:help")],
    ])

def case_view_kb(case_id, index, total):
    rows = []
    if total and total > 1:
        nav = []
        if index > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"nav:{index-1}"))
        nav.append(InlineKeyboardButton(f"{index+1}/{total}", callback_data="noop"))
        if index < total - 1:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"nav:{index+1}"))
        rows.append(nav)
    rows.append([
        InlineKeyboardButton("📝 Add Note", callback_data=f"act:note:{case_id}"),
        InlineKeyboardButton("🔄 Status", callback_data=f"act:status:{case_id}"),
    ])
    rows.append([InlineKeyboardButton("🔙 Main Menu", callback_data="menu:back")])
    return InlineKeyboardMarkup(rows)

def status_kb(case_id):
    rows, row = [], []
    for s in STATUS_OPTIONS:
        code = STATUS_CODES[s]
        btn = InlineKeyboardButton(f"{get_status_emoji(s)} {s}", callback_data=f"set:{case_id}:{code}")
        row.append(btn)
        if len(row) == 2:
            rows.append(row); row = []
    if row: rows.append(row)
    rows.append([InlineKeyboardButton("🔙 Back", callback_data=f"view:{case_id}")])
    return InlineKeyboardMarkup(rows)

def note_templates_kb(case_id):
    rows = []
    row = []
    for code, label in NOTE_TEMPLATE_LABELS.items():
        row.append(InlineKeyboardButton(label, callback_data=f"qn:{code}:{case_id}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row: rows.append(row)
    rows.append([InlineKeyboardButton("✏️ Custom Note", callback_data=f"act:cnote:{case_id}")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data=f"view:{case_id}")])
    return InlineKeyboardMarkup(rows)

def report_kb(total):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"👁 Browse {total} Cases", callback_data="nav:0")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="menu:back")],
    ])

def filter_kb():
    rows = [
        [InlineKeyboardButton("📋 All", callback_data="filter:ALL")],
        [
            InlineKeyboardButton(f"{get_status_emoji('Pending')} Pending", callback_data="filter:PEND"),
            InlineKeyboardButton(f"{get_status_emoji('Approved')} Approved", callback_data="filter:APPR"),
        ],
        [
            InlineKeyboardButton(f"{get_status_emoji('Processing')} Processing", callback_data="filter:PROC"),
            InlineKeyboardButton(f"{get_status_emoji('Contact')} Contact", callback_data="filter:CONT"),
        ],
        [
            InlineKeyboardButton(f"{get_status_emoji('Contact Fail')} Contact Fail", callback_data="filter:CFAIL"),
            InlineKeyboardButton(f"{get_status_emoji('Fail')} Fail", callback_data="filter:FAIL"),
        ],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="menu:back")],
    ]
    return InlineKeyboardMarkup(rows)

# ------------------------ Render ------------------------
async def render_case(query, context, idx):
    results = context.user_data.get("results", [])
    if not results or idx < 0 or idx >= len(results):
        await query.edit_message_text("❌ কোনো কেস নেই।")
        return
    context.user_data["index"] = idx
    case = results[idx]
    total = len(results)
    text = format_single_case(case, index=idx + 1, total=total if total > 1 else None)
    kb = case_view_kb(case.get("case_id"), idx, total)
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.error(f"render_case: {e}")

# ------------------------ Auth ------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_user_blocked(uid):
        await update.message.reply_text("⛔ আপনি স্থায়ীভাবে ব্লক হয়ে গেছেন।")
        return ConversationHandler.END
    if is_user_authenticated(uid):
        await update.message.reply_html(
            "╔═══════════════════════════╗\n"
            "║  👋  <b>WELCOME BACK</b>  ║\n"
            "╚═══════════════════════════╝\n\n"
            "নিচের বাটন থেকে অপশন বেছে নিন বা সরাসরি কেস আইডি / ইউজারনেম / ফোন পাঠান।",
            reply_markup=main_menu_kb(),
        )
        return MAIN_MENU
    await update.message.reply_text("🔐 দয়া করে আপনার ইমেইল লিখুন:")
    return ASK_EMAIL

async def receive_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    email = update.message.text.strip()
    if is_user_blocked(uid):
        await update.message.reply_text("⛔ আপনি ব্লক।")
        return ConversationHandler.END
    if email.lower() != "case@abir.com":
        increment_user_attempts(uid)
        if is_user_blocked(uid):
            await update.message.reply_text("⛔ ৩ বার ভুল — স্থায়ীভাবে ব্লক।")
            return ConversationHandler.END
        await update.message.reply_text(f"❌ ইমেইল ভুল ({get_user_attempts(uid)}/3):")
        return ASK_EMAIL
    await update.message.reply_text("🔑 পাসওয়ার্ড লিখুন:")
    return ASK_PASSWORD

async def receive_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    pwd = update.message.text.strip()
    if is_user_blocked(uid):
        await update.message.reply_text("⛔ আপনি ব্লক।")
        return ConversationHandler.END
    if pwd != "abir.com":
        increment_user_attempts(uid)
        if is_user_blocked(uid):
            await update.message.reply_text("⛔ ৩ বার ভুল — স্থায়ীভাবে ব্লক।")
            return ConversationHandler.END
        await update.message.reply_text(f"❌ পাসওয়ার্ড ভুল ({get_user_attempts(uid)}/3):")
        return ASK_PASSWORD
    authenticate_user(uid)
    reset_user_attempts(uid)
    await update.message.reply_text("✅ লগইন সফল!")
    await update.message.reply_html(
        "╔═══════════════════════════╗\n"
        "║  👋  <b>WELCOME</b>  ║\n"
        "╚═══════════════════════════╝\n\n"
        "নিচের বাটন থেকে অপশন বেছে নিন বা সরাসরি কেস আইডি / ইউজারনেম / ফোন পাঠান।",
        reply_markup=main_menu_kb(),
    )
    return MAIN_MENU

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("new_case", None)
    context.user_data.pop("new_case_step", None)
    await update.message.reply_text("🚫 বাতিল। /start দিয়ে আবার শুরু করুন।")
    return MAIN_MENU

# ------------------------ New Case Prompt ------------------------
async def prompt_new_case_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data["new_case_step"]
    field, label, required = NEW_CASE_FIELDS[step]
    hint = "<i>(Required)</i>" if required else "<i>(Optional — /skip লিখে স্কিপ করুন)</i>"
    text = (
        f"📝 <b>New Case</b> — Step <b>{step+1}/{len(NEW_CASE_FIELDS)}</b>\n\n"
        f"👉 <b>{escape(label)}</b>\n{hint}"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="menu:back")]])
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
    else:
        await update.message.reply_html(text, reply_markup=kb)

async def new_case_start_from_callback(query, context):
    context.user_data["new_case"] = {}
    context.user_data["new_case_step"] = 0
    step = 0
    field, label, required = NEW_CASE_FIELDS[step]
    hint = "<i>(Required)</i>" if required else "<i>(Optional — /skip লিখে স্কিপ করুন)</i>"
    text = (
        f"📝 <b>New Case</b> — Step <b>1/{len(NEW_CASE_FIELDS)}</b>\n\n"
        f"👉 <b>{escape(label)}</b>\n{hint}"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="menu:back")]])
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass

async def new_case_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    step = context.user_data.get("new_case_step", 0)
    if step >= len(NEW_CASE_FIELDS):
        return MAIN_MENU

    field, label, required = NEW_CASE_FIELDS[step]

    if text.lower() in ("/skip", "skip", "-", "না", "no"):
        if required:
            await update.message.reply_html(f"❌ <b>{escape(label)}</b> required — আবার পাঠান:")
            return NEW_CASE_INPUT
        value = None
    else:
        value = text

    context.user_data["new_case"][field] = value
    context.user_data["new_case_step"] = step + 1

    if context.user_data["new_case_step"] >= len(NEW_CASE_FIELDS):
        # Save!
        data = context.user_data.pop("new_case", {})
        context.user_data.pop("new_case_step", None)
        new_case = create_new_case(data)
        if not new_case:
            await update.message.reply_text("❌ কেস তৈরি করা যায়নি। পরে আবার চেষ্টা করুন।")
            return MAIN_MENU

        context.user_data["results"] = [new_case]
        context.user_data["index"] = 0
        await update.message.reply_html("✅ <b>New Case Created!</b>\n\n" + format_single_case(new_case),
                                        reply_markup=case_view_kb(new_case.get("case_id"), 0, 1))
        return MAIN_MENU

    await prompt_new_case_field(update, context)
    return NEW_CASE_INPUT

# ------------------------ Menu Callback ------------------------
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data == "noop":
        await query.answer()
        return MAIN_MENU

    await query.answer()

    if data == "menu:back":
        context.user_data.pop("new_case", None)
        context.user_data.pop("new_case_step", None)
        try:
            await query.edit_message_text(
                "🏠 <b>Main Menu</b>\n\nনিচের বাটন থেকে অপশন বেছে নিন।",
                parse_mode="HTML", reply_markup=main_menu_kb(),
            )
        except Exception: pass
        return MAIN_MENU

    if data == "menu:search":
        try:
            await query.edit_message_text(
                "🔍 <b>Master Search</b>\n\n"
                "যেকোনো একটা পাঠান — আমি সব ফিল্ডে খুঁজব:\n"
                "  ▸ Case ID  ▸ Username  ▸ Phone\n"
                "  ▸ Platform  ▸ Password  ▸ Amount\n"
                "  ▸ Notes-এ কিছু লেখা\n\n"
                "<i>বাতিল করতে /cancel</i>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Main Menu", callback_data="menu:back")]]),
            )
        except Exception: pass
        return MAIN_MENU

    if data == "menu:help":
        try:
            await query.edit_message_text(
                "❓ <b>Help</b>\n\n"
                "• <b>Last 7 Days Report</b> — শেষ ৭ দিনের কেস\n"
                "• <b>Search Case</b> — Master search (সব ফিল্ডে খোঁজে)\n"
                "• <b>Filter by Status</b> — Approved / Pending / Contact / Contact Fail / Fail\n"
                "• <b>New Case</b> — নতুন কেস তৈরি\n"
                "• <b>Statistics</b> — সব স্ট্যাটাস কাউন্ট\n\n"
                "কেস ভিউতে: ◀️▶️ Prev/Next, 📝 Add Note, 🔄 Status",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Main Menu", callback_data="menu:back")]]),
            )
        except Exception: pass
        return MAIN_MENU

    if data == "menu:7d":
        await show_7day_report(query, context)
        return MAIN_MENU

    if data == "menu:stats":
        await show_statistics(query, context)
        return MAIN_MENU

    if data == "menu:filter":
        try:
            await query.edit_message_text(
                "🗂 <b>Filter by Status</b>\n\nকোন স্ট্যাটাসের কেস দেখতে চান?",
                parse_mode="HTML", reply_markup=filter_kb(),
            )
        except Exception: pass
        return MAIN_MENU

    if data == "menu:newcase":
        await new_case_start_from_callback(query, context)
        return NEW_CASE_INPUT

    # ---- Filter ----
    if data.startswith("filter:"):
        code = data.split(":", 1)[1]
        if code == "ALL":
            cases = search_all_cases()
            label = "All Statuses"
        else:
            status = REVERSE_STATUS.get(code, "Pending")
            cases = search_by_status(status)
            label = f"{get_status_emoji(status)} {status}"

        if not cases:
            try:
                await query.edit_message_text(
                    f"🗂 <b>{escape(label)}</b>\n\n<i>কোনো কেস নেই।</i>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu:filter")]]),
                )
            except Exception: pass
            return MAIN_MENU

        context.user_data["results"] = cases
        context.user_data["index"] = 0
        await render_case(query, context, 0)
        return MAIN_MENU

    # ---- Pagination ----
    if data.startswith("nav:"):
        parts = data.split(":")
        if len(parts) == 2:
            idx = int(parts[1])
            await render_case(query, context, idx)
        return MAIN_MENU

    # ---- Notes menu (templates) ----
    if data.startswith("act:note:"):
        case_id = data.split(":", 2)[2]
        try:
            await query.edit_message_text(
                f"📝 <b>Add Note</b>\n\n"
                f"Case: <code>{escape(case_id)}</code>\n\n"
                f"নিচের টেমপ্লেট থেকে বেছে নিন বা Custom Note দিন:",
                parse_mode="HTML",
                reply_markup=note_templates_kb(case_id),
            )
        except Exception: pass
        return MAIN_MENU

    # ---- Quick note templates ----
    if data.startswith("qn:"):
        parts = data.split(":", 2)
        if len(parts) != 3: return MAIN_MENU
        _, code, case_id = parts
        text = NOTE_TEMPLATES.get(code)
        if not text:
            await query.answer("❌ Unknown", show_alert=True)
            return MAIN_MENU
        ok = append_note(case_id, text)
        if ok:
            await query.answer(f"✅ Note added: {text[:20]}", show_alert=False)
            fresh = search_by_case_id(case_id)
            if fresh:
                results = context.user_data.get("results", [])
                idx = 0; found = False
                for i, c in enumerate(results):
                    if c.get("case_id") == case_id:
                        results[i] = fresh; idx = i; found = True; break
                if not found:
                    results = [fresh]; idx = 0
                context.user_data["results"] = results
                context.user_data["index"] = idx
                await render_case(query, context, idx)
        else:
            await query.answer("❌ Failed", show_alert=True)
        return MAIN_MENU

    # ---- Custom note ----
    if data.startswith("act:cnote:"):
        case_id = data.split(":", 2)[2]
        context.user_data["note_case_id"] = case_id
        try:
            await query.edit_message_text(
                f"✏️ <b>Custom Note</b>\n\n"
                f"Case: <code>{escape(case_id)}</code>\n\n"
                f"নোটের টেক্সট পাঠান:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Cancel", callback_data=f"view:{case_id}")]
                ]),
            )
        except Exception: pass
        return ASK_NOTE

    # ---- Status ----
    if data.startswith("act:status:"):
        case_id = data.split(":", 2)[2]
        case = search_by_case_id(case_id)
        current = case.get('status') if case else "N/A"
        try:
            await query.edit_message_text(
                f"🔄 <b>Update Status</b>\n\n"
                f"Case: <code>{escape(case_id)}</code>\n"
                f"Current: {get_status_emoji(current)} <b>{escape(str(current))}</b>\n\n"
                f"নতুন স্ট্যাটাস বেছে নিন:",
                parse_mode="HTML", reply_markup=status_kb(case_id),
            )
        except Exception: pass
        return MAIN_MENU

    if data.startswith("set:"):
        parts = data.split(":", 2)
        if len(parts) != 3: return MAIN_MENU
        _, case_id, code = parts
        new_status = REVERSE_STATUS.get(code)
        if not new_status:
            await query.answer("❌ Unknown", show_alert=True); return MAIN_MENU
        ok = update_case_status(case_id, new_status, query.from_user.id)
        if ok:
            await query.answer(f"✅ {new_status}", show_alert=False)
            results = context.user_data.get("results", [])
            idx = context.user_data.get("index", 0)
            for i, c in enumerate(results):
                if c.get("case_id") == case_id:
                    fresh = search_by_case_id(case_id)
                    if fresh: results[i] = fresh
                    idx = i; break
            context.user_data["results"] = results
            context.user_data["index"] = idx
            await render_case(query, context, idx)
        else:
            await query.answer("❌ Update failed", show_alert=True)
        return MAIN_MENU

    if data.startswith("view:"):
        case_id = data.split(":", 1)[1]
        results = context.user_data.get("results", [])
        for i, c in enumerate(results):
            if c.get("case_id") == case_id:
                await render_case(query, context, i); return MAIN_MENU
        fresh = search_by_case_id(case_id)
        if fresh:
            context.user_data["results"] = [fresh]
            context.user_data["index"] = 0
            await render_case(query, context, 0)
        return MAIN_MENU

    return MAIN_MENU

# ------------------------ Reports ------------------------
async def show_7day_report(query, context):
    cases = search_last_7_days()
    if not cases:
        try:
            await query.edit_message_text(
                "📅 <b>Last 7 Days Report</b>\n\n<i>কোনো কেস নেই।</i>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Main Menu", callback_data="menu:back")]]),
            )
        except Exception: pass
        return

    counts, total_amount = {}, 0.0
    for c in cases:
        s = c.get('status') or 'Unknown'
        counts[s] = counts.get(s, 0) + 1
        try: total_amount += float(c.get('amount') or 0)
        except (ValueError, TypeError): pass

    lines = [
        "╔═══════════════════════════════╗",
        "║  📅  <b>LAST 7 DAYS REPORT</b>",
        "╚═══════════════════════════════╝",
        "",
        f"📊 <b>Total Cases:</b> <code>{len(cases)}</code>",
        f"💰 <b>Total Amount:</b> <code>{total_amount:,.2f}</code>",
        "",
        "┏━━━ 📌 <b>BY STATUS</b> ━━━┓",
    ]
    for s, cnt in sorted(counts.items(), key=lambda x: -x[1]):
        lines.append(f"   {get_status_emoji(s)} {escape(s)} : <b>{cnt}</b>")
    lines += [
        "┗━━━━━━━━━━━━━━━━━━━━━━━┛",
        "",
        "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬",
        "<i>নিচের বাটনে ক্লিক করে সব কেস ব্রাউজ করুন।</i>",
    ]
    context.user_data["results"] = cases
    context.user_data["index"] = 0
    try:
        await query.edit_message_text("\n".join(lines), parse_mode="HTML",
                                      reply_markup=report_kb(len(cases)))
    except Exception as e:
        logger.error(f"7d report render: {e}")

async def show_statistics(query, context):
    try:
        r = supabase.table("withdrawals").select("status").limit(10000).execute()
        rows = r.data or []
    except Exception as e:
        logger.error(f"stats: {e}"); rows = []

    counts = {}
    for row in rows:
        s = row.get('status') or 'Unknown'
        counts[s] = counts.get(s, 0) + 1

    lines = [
        "╔═══════════════════════════════╗",
        "║  📊  <b>ALL-TIME STATISTICS</b>",
        "╚═══════════════════════════════╝",
        "",
        f"🗂️ <b>Total Cases:</b> <code>{len(rows)}</code>",
        "",
        "┏━━━ 📌 <b>BY STATUS</b> ━━━┓",
    ]
    for s, cnt in sorted(counts.items(), key=lambda x: -x[1]):
        lines.append(f"   {get_status_emoji(s)} {escape(s)} : <b>{cnt}</b>")
    lines.append("┗━━━━━━━━━━━━━━━━━━━━━━━┛")

    try:
        await query.edit_message_text("\n".join(lines), parse_mode="HTML",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Main Menu", callback_data="menu:back")]]))
    except Exception as e:
        logger.error(f"stats render: {e}")

# ------------------------ Direct Search (Master) ------------------------
async def direct_search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # Smart routing: exact case_id or phone → fast path; otherwise master search
    if is_case_id(text):
        case = search_by_case_id(text)
        results = [case] if case else []
        label = f"Case ID: <code>{escape(text)}</code>"
    elif is_phone_number(text):
        results = search_by_phone(text)
        if not results:
            results = master_search(text)  # fallback to master
        label = f"Phone: <code>{escape(text)}</code>"
    else:
        results = master_search(text)
        label = f"Master search: <code>{escape(text)}</code>"

    if not results:
        await update.message.reply_html(
            f"❌ <b>No results</b> for {label}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Main Menu", callback_data="menu:back")]]),
        )
        return MAIN_MENU

    context.user_data["results"] = results
    context.user_data["index"] = 0
    total = len(results)
    case = results[0]
    text_out = format_single_case(case, index=1, total=total if total > 1 else None)
    kb = case_view_kb(case.get("case_id"), 0, total)
    await update.message.reply_html(text_out, reply_markup=kb)
    return MAIN_MENU

# ------------------------ Custom Note Receipt ------------------------
async def receive_custom_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    note_text = update.message.text.strip()
    case_id = context.user_data.get("note_case_id")
    if not case_id:
        await update.message.reply_text("❌ Session expired. /start")
        return MAIN_MENU
    ok = append_note(case_id, note_text)
    if not ok:
        await update.message.reply_text("❌ নোট যোগ করা যায়নি।")
        return MAIN_MENU
    await update.message.reply_html(f"✅ নোট যোগ হয়েছে — <code>{escape(case_id)}</code>")
    fresh = search_by_case_id(case_id)
    if fresh:
        results = context.user_data.get("results", [])
        idx = 0; found = False
        for i, c in enumerate(results):
            if c.get("case_id") == case_id:
                results[i] = fresh; idx = i; found = True; break
        if not found:
            results = [fresh]; idx = 0
        context.user_data["results"] = results
        context.user_data["index"] = idx
        total = len(results)
        text_out = format_single_case(fresh, index=idx + 1, total=total if total > 1 else None)
        kb = case_view_kb(case_id, idx, total)
        await update.message.reply_html(text_out, reply_markup=kb)
    return MAIN_MENU

# ------------------------ Error ------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Update error:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ সমস্যা হয়েছে, আবার চেষ্টা করুন।")
        except Exception: pass

# ------------------------ Main ------------------------
def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    common_cb = [CallbackQueryHandler(menu_callback)]
    common_cmd = [CommandHandler("cancel", cancel_command)]

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start_command)],
        states={
            ASK_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_email)] + common_cmd,
            ASK_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password)] + common_cmd,
            MAIN_MENU: [
                CallbackQueryHandler(menu_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, direct_search_handler),
            ] + common_cmd,
            ASK_NOTE: [
                CallbackQueryHandler(menu_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_custom_note),
            ] + common_cmd,
            NEW_CASE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, new_case_input),
            ] + common_cb + common_cmd,
        },
        fallbacks=[
            CommandHandler("cancel", cancel_command),
            CommandHandler("start", start_command),
        ],
        allow_reentry=True,
    )
    application.add_handler(conv)
    application.add_error_handler(error_handler)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        logger.info("বট চলছে...")
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        loop.close()

if __name__ == "__main__":
    main()
