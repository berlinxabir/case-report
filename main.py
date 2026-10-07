import os
import logging
import re
import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from html import escape
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
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

# ------------------------ লগিং ------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ------------------------ Supabase ------------------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
logger.info("Supabase ক্লায়েন্ট রেডি")

# ------------------------ কনভারসেশন স্টেট ------------------------
ASK_EMAIL, ASK_PASSWORD = range(2)

# ------------------------ ফাইল হ্যান্ডলিং ------------------------
BLOCKED_FILE = "blocked_users.json"
AUTH_FILE = "authenticated_users.json"
ATTEMPT_FILE = "login_attempts.json"

def load_blocked() -> set:
    try:
        with open(BLOCKED_FILE, "r") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_blocked(blocked: set):
    with open(BLOCKED_FILE, "w") as f:
        json.dump(list(blocked), f)

def is_user_blocked(user_id: int) -> bool:
    return str(user_id) in load_blocked()

def block_user(user_id: int):
    blocked = load_blocked()
    blocked.add(str(user_id))
    save_blocked(blocked)

def load_authenticated() -> set:
    try:
        with open(AUTH_FILE, "r") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_authenticated(auth: set):
    with open(AUTH_FILE, "w") as f:
        json.dump(list(auth), f)

def is_user_authenticated(user_id: int) -> bool:
    return str(user_id) in load_authenticated()

def authenticate_user(user_id: int):
    auth = load_authenticated()
    auth.add(str(user_id))
    save_authenticated(auth)

def load_attempts() -> dict:
    try:
        with open(ATTEMPT_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_attempts(attempts: dict):
    with open(ATTEMPT_FILE, "w") as f:
        json.dump(attempts, f)

def get_user_attempts(user_id: int) -> int:
    attempts = load_attempts()
    return attempts.get(str(user_id), 0)

def increment_user_attempts(user_id: int):
    attempts = load_attempts()
    uid = str(user_id)
    attempts[uid] = attempts.get(uid, 0) + 1
    save_attempts(attempts)
    if attempts[uid] >= 3:
        block_user(user_id)

def reset_user_attempts(user_id: int):
    attempts = load_attempts()
    uid = str(user_id)
    if uid in attempts:
        del attempts[uid]
        save_attempts(attempts)

# ------------------------ হেল্পার ------------------------
def is_case_id(text: str) -> bool:
    return bool(re.fullmatch(r'W\d{8,}', text, re.IGNORECASE))

def is_phone_number(text: str) -> bool:
    cleaned = re.sub(r'[\s\-\(\)]', '', text)
    return bool(re.fullmatch(r'\+?\d{7,15}', cleaned))

def safe(value) -> str:
    if value is None or value == "":
        return "—"
    return escape(str(value))

def format_datetime(iso_str: str) -> str:
    if not iso_str:
        return "N/A"
    try:
        if iso_str.endswith('Z'):
            iso_str = iso_str[:-1] + '+00:00'
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        dhaka_tz = ZoneInfo("Asia/Dhaka")
        local_dt = dt.astimezone(dhaka_tz)
        return local_dt.strftime("%d %b %Y, %I:%M %p").lower()
    except Exception:
        return iso_str

def split_datetime(iso_str: str) -> tuple[str, str]:
    """(date_str, time_str) রিটার্ন করে আলাদা লাইনে দেখানোর জন্য"""
    formatted = format_datetime(iso_str)
    if "," in formatted:
        d, t = formatted.split(",", 1)
        return d.strip(), t.strip()
    return formatted, "—"

# ------------------------ ডাটাবেস অনুসন্ধান ------------------------
def search_by_case_id(case_id: str) -> dict | None:
    try:
        response = supabase.table("withdrawals").select("*").eq("case_id", case_id.upper()).execute()
        return response.data[0] if response.data else None
    except Exception as e:
        logger.error(f"কেস আইডি অনুসন্ধানে সমস্যা: {e}")
        return None

def search_by_phone(phone: str) -> list[dict]:
    try:
        response = supabase.table("withdrawals").select("*").ilike("phone_number", phone).execute()
        return response.data if response.data else []
    except Exception as e:
        logger.error(f"ফোন অনুসন্ধানে সমস্যা: {e}")
        return []

def search_by_username(username: str) -> list[dict]:
    try:
        response = supabase.table("withdrawals").select("*").ilike("username", username).execute()
        return response.data if response.data else []
    except Exception as e:
        logger.error(f"ইউজারনেম অনুসন্ধানে সমস্যা: {e}")
        return []

# ------------------------ স্ট্যাটিসটিকস পার্সিং ------------------------
def extract_usernames_from_stats(text: str) -> list[str]:
    matches = re.findall(r'`(\w+):[^`]+`', text)
    return list(dict.fromkeys(matches))

def is_stats_message(text: str) -> bool:
    indicators = ["PARTIAL RESULTS", "STATISTICS", "TOP 5 BALANCES"]
    return any(ind in text for ind in indicators)

# ------------------------ স্ট্যাটাস ব্যাজ ------------------------
def get_status_badge(status: str) -> str:
    s = (status or "").strip().lower()
    if "reject" in s or "cancel" in s or "fail" in s:
        return "🔴 REJECTED"
    if "approv" in s or "success" in s or "complete" in s or "paid" in s:
        return "🟢 APPROVED"
    if "pending" in s or "process" in s or "wait" in s:
        return "🟡 PENDING"
    if not s:
        return "⚪ UNKNOWN"
    return f"⚪ {status.upper()}"

# ------------------------ ফরম্যাটিং ------------------------
def format_single_case(case: dict, index: int | None = None) -> str:
    date_str, time_str = split_datetime(case.get('created_at', ''))
    status_badge = get_status_badge(case.get('status', ''))

    # নাম্বার কার্ড লেবেল
    if index is not None:
        title_line = f"║  🗂️  <b>CASE #{index}</b>"
    else:
        title_line = "║  🗂️  <b>CASE DETAILS</b>"

    return (
        "╔═══════════════════════════════╗\n"
        f"{title_line}\n"
        "╚═══════════════════════════════╝\n"
        "\n"
        "┏━━━ 👤 <b>ACCOUNT</b> ━━━┓\n"
        f"   🔑 <b>Username</b>  :  <code>{safe(case.get('username'))}</code>\n"
        f"   🔒 <b>Password</b>  :  <code>{safe(case.get('password'))}</code>\n"
        f"   🆔 <b>Case ID</b>    :  <code>{safe(case.get('case_id'))}</code>\n"
        f"   📞 <b>Phone</b>       :  <code>{safe(case.get('phone_number'))}</code>\n"
        "┗━━━━━━━━━━━━━━━━━━━━━━━┛\n"
        "\n"
        "┏━━━ 💰 <b>TRANSACTION</b> ━━━┓\n"
        f"   📱 <b>Platform</b>   :  {safe(case.get('platform'))}\n"
        f"   📦 <b>Type</b>          :  {safe(case.get('type'))}\n"
        f"   💳 <b>Payment</b>    :  {safe(case.get('payment_method'))}\n"
        f"   💵 <b>Amount</b>      :  <b>{safe(case.get('amount'))}</b>\n"
        "┗━━━━━━━━━━━━━━━━━━━━━━━┛\n"
        "\n"
        "┏━━━ 📌 <b>STATUS</b> ━━━┓\n"
        f"   {status_badge}\n"
        f"   📝 <i>{safe(case.get('notes'))}</i>\n"
        "┗━━━━━━━━━━━━━━━━━━━━━━━┛\n"
        "\n"
        "┏━━━ 🕒 <b>DATE &amp; TIME</b> ━━━┓\n"
        f"   📅 <b>Date</b>  :  <code>{date_str}</code>\n"
        f"   ⏰ <b>Time</b>  :  <code>{time_str}</code>\n"
        "┗━━━━━━━━━━━━━━━━━━━━━━━┛\n"
        "\n"
        "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬"
    )

def format_multi_case_header(count: int, search_type: str) -> str:
    return (
        "╔═══════════════════════════════╗\n"
        f"║   📊  <b>{count} RESULTS FOUND</b>\n"
        "╚═══════════════════════════════╝\n"
        f"<i>🔎 {search_type}</i>\n"
        "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
    )

# ------------------------ অথেন্টিকেশন কনভারসেশন ------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    if is_user_blocked(user_id):
        await update.message.reply_text("⛔ আপনি স্থায়ীভাবে ব্লক হয়ে গেছেন।")
        return ConversationHandler.END

    if is_user_authenticated(user_id):
        await update.message.reply_html(
            "╔═══════════════════════════╗\n"
            "║  👋  <b>WELCOME BACK</b>  ║\n"
            "╚═══════════════════════════╝\n\n"
            "🔎 কেস আইডি / ইউজারনেম / ফোন পাঠান।\n\n"
            "উদাহরণ:\n"
            "  ▸ <code>W009201204250</code>\n"
            "  ▸ <code>bxfarha</code>\n"
            "  ▸ <code>01307241916</code>\n\n"
            "📊 পরিসংখ্যান রিপোর্ট ফরওয়ার্ড করলে স্বয়ংক্রিয়ভাবে সব কেস দেখাবে।"
        )
        return ConversationHandler.END

    await update.message.reply_text("🔐 দয়া করে আপনার ইমেইল লিখুন:")
    return ASK_EMAIL

async def receive_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    email = update.message.text.strip()

    if is_user_blocked(user_id):
        await update.message.reply_text("⛔ আপনি ব্লক হয়ে গেছেন।")
        return ConversationHandler.END

    if email.lower() != "case@abir.com":
        increment_user_attempts(user_id)
        attempts = get_user_attempts(user_id)
        if is_user_blocked(user_id):
            await update.message.reply_text("⛔ ৩ বার ভুল দেওয়ায় আপনি স্থায়ীভাবে ব্লক হয়ে গেছেন।")
            return ConversationHandler.END
        await update.message.reply_text(f"❌ ইমেইল ভুল। আবার চেষ্টা করুন ({attempts}/3):")
        return ASK_EMAIL

    await update.message.reply_text("🔑 এখন পাসওয়ার্ড লিখুন:")
    return ASK_PASSWORD

async def receive_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    password = update.message.text.strip()

    if is_user_blocked(user_id):
        await update.message.reply_text("⛔ আপনি ব্লক হয়ে গেছেন।")
        return ConversationHandler.END

    if password != "abir.com":
        increment_user_attempts(user_id)
        attempts = get_user_attempts(user_id)
        if is_user_blocked(user_id):
            await update.message.reply_text("⛔ ৩ বার ভুল দেওয়ায় আপনি স্থায়ীভাবে ব্লক হয়ে গেছেন।")
            return ConversationHandler.END
        await update.message.reply_text(f"❌ পাসওয়ার্ড ভুল। আবার চেষ্টা করুন ({attempts}/3):")
        return ASK_PASSWORD

    authenticate_user(user_id)
    reset_user_attempts(user_id)
    await update.message.reply_text("✅ লগইন সফল!")
    await update.message.reply_html(
        "╔═══════════════════════════╗\n"
        "║  👋  <b>WELCOME</b>  ║\n"
        "╚═══════════════════════════╝\n\n"
        "🔎 কেস আইডি / ইউজারনেম / ফোন পাঠান।\n\n"
        "উদাহরণ:\n"
        "  ▸ <code>W009201204250</code>\n"
        "  ▸ <code>bxfarha</code>\n"
        "  ▸ <code>01307241916</code>\n\n"
        "📊 পরিসংখ্যান রিপোর্ট ফরওয়ার্ড করলে স্বয়ংক্রিয়ভাবে সব কেস দেখাবে।"
    )
    return ConversationHandler.END

async def cancel_auth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 অথেন্টিকেশন বাতিল। /start দিয়ে আবার চেষ্টা করুন।")
    return ConversationHandler.END

# ------------------------ চাঙ্কড সেন্ড হেল্পার ------------------------
async def send_long_message(update: Update, text: str, limit: int = 3800):
    if len(text) <= limit:
        await update.message.reply_html(text)
        return

    parts = text.split("\n\n")
    chunk = ""
    for part in parts:
        piece = part + "\n\n"
        if len(chunk) + len(piece) > limit:
            if chunk.strip():
                await update.message.reply_html(chunk.strip())
            chunk = piece
        else:
            chunk += piece
    if chunk.strip():
        await update.message.reply_html(chunk.strip())

# ------------------------ মেসেজ হ্যান্ডলার ------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    text = update.message.text.strip()

    if is_user_blocked(user_id):
        await update.message.reply_text("⛔ আপনি ব্লক হয়ে গেছেন।")
        return

    if not is_user_authenticated(user_id):
        await update.message.reply_text("🔐 অনুগ্রহ করে /start দিয়ে লগইন করুন।")
        return

    await update.message.chat.send_action(action="typing")

    # ---------- স্ট্যাটস ----------
    if is_stats_message(text):
        usernames = extract_usernames_from_stats(text)
        if not usernames:
            await update.message.reply_text("❌ মেসেজে কোনো ইউজারনেম পাওয়া যায়নি।")
            return

        all_results = []
        seen_ids = set()
        for uname in usernames:
            for c in search_by_username(uname):
                cid = c.get("case_id")
                if cid and cid not in seen_ids:
                    seen_ids.add(cid)
                    all_results.append(c)

        if not all_results:
            await update.message.reply_text("❌ কোনো কেস পাওয়া যায়নি।")
            return

        header = format_multi_case_header(len(all_results), f"স্ট্যাটস রিপোর্ট ({len(usernames)} ইউজারনেম)")
        parts = [format_single_case(c, index=i + 1) for i, c in enumerate(all_results)]
        await send_long_message(update, header + "\n\n" + "\n\n".join(parts))
        return

    # ---------- কেস আইডি ----------
    if is_case_id(text):
        case = search_by_case_id(text)
        if case:
            await update.message.reply_html(format_single_case(case))
        else:
            await update.message.reply_html("❌ এই কেস আইডি পাওয়া যায়নি।")
        return

    # ---------- ফোন / ইউজারনেম ----------
    if is_phone_number(text):
        results = search_by_phone(text)
        search_type = "ফোন নম্বর"
    else:
        results = search_by_username(text)
        search_type = "ইউজারনেম"

    if not results:
        await update.message.reply_html(f"❌ এই {search_type} দিয়ে কোনো কেস পাওয়া যায়নি।")
        return

    if len(results) == 1:
        await update.message.reply_html(format_single_case(results[0]))
        return

    limit = 5
    shown = results[:limit]
    header = format_multi_case_header(len(results), f"{search_type}: <code>{safe(text)}</code>")
    parts = [format_single_case(c, index=i + 1) for i, c in enumerate(shown)]
    full_text = header + "\n\n" + "\n\n".join(parts)
    if len(results) > limit:
        full_text += f"\n\n<i>… আরও {len(results) - limit} টি কেস আছে।</i>"

    await send_long_message(update, full_text)

# ------------------------ এরর হ্যান্ডলার ------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(msg="আপডেট প্রসেস করতে সমস্যা:", exc_info=context.error)
    if update and isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ একটি সমস্যা হয়েছে, পরে চেষ্টা করুন।")
        except Exception:
            pass

# ------------------------ মেইন ------------------------
def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    auth_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start_command)],
        states={
            ASK_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_email)],
            ASK_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password)],
        },
        fallbacks=[CommandHandler("cancel", cancel_auth)],
    )
    application.add_handler(auth_conv)

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
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
