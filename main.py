import os
import logging
import re
import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo
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

# ------------------------ এনভায়রনমেন্ট ------------------------
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY")

if not all([TELEGRAM_TOKEN, SUPABASE_URL, SUPABASE_KEY]):
    raise ValueError(".env ফাইলে সব ভেরিয়েবল সেট করুন")

# ------------------------ লগিং ------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ------------------------ Supabase ------------------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
logger.info("Supabase ক্লায়েন্ট রেডি")

# ------------------------ কনভারসেশন স্টেট (অথেন্টিকেশন) ------------------------
ASK_EMAIL, ASK_PASSWORD = range(2)

# ------------------------ ফাইল হ্যান্ডলিং (ব্লক ও অথেন্টিকেশন) ------------------------
BLOCKED_FILE = "blocked_users.json"
AUTH_FILE = "authenticated_users.json"
ATTEMPT_FILE = "login_attempts.json"      # চেষ্টার হিসাব রাখবে (বট রিস্টার্টে মুছে না)

# ---------- ব্লকড ইউজার ----------
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

# ---------- অথেন্টিকেটেড ইউজার ----------
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

# ---------- লগইন চেষ্টা ----------
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
    # ৩ বার হলে ব্লক
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

def format_datetime(iso_str: str) -> str:
    """ISO স্ট্রিংকে বাংলাদেশ সময়ে '04 Jun 2026, 08:33 am' ফরম্যাটে রূপান্তর"""
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

# ------------------------ ডাটাবেস অনুসন্ধান ------------------------
def search_by_case_id(case_id: str) -> dict | None:
    try:
        response = supabase.table("withdrawals").select("*").eq("case_id", case_id.upper()).execute()
        if response.data:
            return response.data[0]
        return None
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

# ------------------------ ফরম্যাটিং ------------------------
def format_single_case(case: dict) -> str:
    created = format_datetime(case.get('created_at', ''))
    return (
        f"📋 <b>কেস আইডি:</b> {case.get('case_id', 'N/A')}\n"
        f"👤 <b>ইউজারনেম:</b> {case.get('username', 'N/A')}\n"
        f"📞 <b>ফোন:</b> {case.get('phone_number', 'N/A')}\n"
        f"📱 <b>প্ল্যাটফর্ম:</b> {case.get('platform', 'N/A')}\n"
        f"📦 <b>টাইপ:</b> {case.get('type', 'N/A')}\n"
        f"💳 <b>পেমেন্ট মেথড:</b> {case.get('payment_method', 'N/A')}\n"
        f"💰 <b>অ্যামাউন্ট:</b> {case.get('amount', 'N/A')}\n"
        f"📌 <b>স্ট্যাটাস:</b> {case.get('status', 'N/A')}\n"
        f"📝 <b>নোটস:</b> {case.get('notes', 'N/A')}\n"
        f"🕒 <b>তৈরি হয়েছে:</b> {created}"
    )

# ------------------------ অথেন্টিকেশন কনভারসেশন ------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    # ব্লকড হলে কিছু না
    if is_user_blocked(user_id):
        await update.message.reply_text("⛔ আপনি স্থায়ীভাবে ব্লক হয়ে গেছেন।")
        return ConversationHandler.END

    # ইতিমধ্যে লগইন করা থাকলে সরাসরি মেনু
    if is_user_authenticated(user_id):
        await update.message.reply_html(
            "👋 <b>স্বাগতম!</b>\n\n"
            "আপনি সরাসরি কেস আইডি, ইউজারনেম বা ফোন নম্বর পাঠাতে পারেন।\n"
            "উদাহরণ:\n"
            "<code>W009201204250</code>\n"
            "<code>bxfarha</code>\n"
            "<code>01307241916</code>\n\n"
            "পরিসংখ্যান রিপোর্ট ফরওয়ার্ড করলে স্বয়ংক্রিয়ভাবে ইউজারনেম বের করে সব কেস দেখাবে।"
        )
        return ConversationHandler.END

    # লগইন করানো শুরু
    await update.message.reply_text("🔐 দয়া করে আপনার ইমেইল লিখুন:")
    return ASK_EMAIL

async def receive_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    email = update.message.text.strip()

    if is_user_blocked(user_id):
        await update.message.reply_text("⛔ আপনি ব্লক হয়ে গেছেন।")
        return ConversationHandler.END

    if email.lower() != "case@abir.com":
        increment_user_attempts(user_id)
        attempts = get_user_attempts(user_id)
        if is_user_blocked(user_id):
            await update.message.reply_text("⛔ ৩ বার ভুল ইমেইল/পাসওয়ার্ড দেওয়ায় আপনি স্থায়ীভাবে ব্লক হয়ে গেছেন।")
            return ConversationHandler.END
        await update.message.reply_text(f"❌ ইমেইল ভুল। আবার চেষ্টা করুন ({attempts}/3):")
        return ASK_EMAIL

    # ইমেইল সঠিক
    await update.message.reply_text("🔑 এখন পাসওয়ার্ড লিখুন:")
    return ASK_PASSWORD

async def receive_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    password = update.message.text.strip()

    if is_user_blocked(user_id):
        await update.message.reply_text("⛔ আপনি ব্লক হয়ে গেছেন।")
        return ConversationHandler.END

    if password != "abir.com":
        increment_user_attempts(user_id)
        attempts = get_user_attempts(user_id)
        if is_user_blocked(user_id):
            await update.message.reply_text("⛔ ৩ বার ভুল ইমেইল/পাসওয়ার্ড দেওয়ায় আপনি স্থায়ীভাবে ব্লক হয়ে গেছেন।")
            return ConversationHandler.END
        await update.message.reply_text(f"❌ পাসওয়ার্ড ভুল। আবার চেষ্টা করুন ({attempts}/3):")
        return ASK_PASSWORD

    # সফল লগইন
    authenticate_user(user_id)
    reset_user_attempts(user_id)      # সফল হলে চেষ্টার কাউন্টার রিসেট
    await update.message.reply_text("✅ লগইন সফল! এখন আপনি কেস অনুসন্ধান করতে পারবেন।")
    # মেনু দেখানো
    await update.message.reply_html(
        "👋 <b>স্বাগতম!</b>\n\n"
        "আপনি সরাসরি কেস আইডি, ইউজারনেম বা ফোন নম্বর পাঠাতে পারেন।\n"
        "উদাহরণ:\n"
        "<code>W009201204250</code>\n"
        "<code>bxfarha</code>\n"
        "<code>01307241916</code>\n\n"
        "পরিসংখ্যান রিপোর্ট ফরওয়ার্ড করলে স্বয়ংক্রিয়ভাবে ইউজারনেম বের করে সব কেস দেখাবে।"
    )
    return ConversationHandler.END

async def cancel_auth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 অথেন্টিকেশন বাতিল করা হয়েছে। /start দিয়ে আবার চেষ্টা করুন।")
    return ConversationHandler.END

# ------------------------ মেসেজ হ্যান্ডলার (সার্চ) ------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    text = update.message.text.strip()

    # ব্লক চেক
    if is_user_blocked(user_id):
        await update.message.reply_text("⛔ আপনি ব্লক হয়ে গেছেন।")
        return

    # অথেন্টিকেটেড কিনা
    if not is_user_authenticated(user_id):
        await update.message.reply_text("🔐 অনুগ্রহ করে /start দিয়ে লগইন করুন।")
        return

    # ---------- মূল সার্চ লজিক ----------
    await update.message.chat.send_action(action="typing")

    # পরিসংখ্যান মেসেজ
    if is_stats_message(text):
        usernames = extract_usernames_from_stats(text)
        if not usernames:
            await update.message.reply_text("❌ মেসেজে কোনো ইউজারনেম খুঁজে পাওয়া যায়নি।")
            return
        all_results = []
        for user_name in usernames:
            cases = search_by_username(user_name)
            if cases:
                all_results.extend(cases)
        if not all_results:
            await update.message.reply_text("❌ কোনো ইউজারনেমের জন্য কেস পাওয়া যায়নি।")
            return
        output_parts = [format_single_case(c) for c in all_results]
        chunk = ""
        for part in output_parts:
            if len(chunk) + len(part) > 3800:
                await update.message.reply_html(chunk)
                chunk = part
            else:
                chunk += "\n\n---\n\n" + part if chunk else part
        if chunk:
            await update.message.reply_html(chunk)
        return

    # কেস আইডি
    if is_case_id(text):
        case = search_by_case_id(text)
        if case:
            await update.message.reply_html(format_single_case(case))
        else:
            await update.message.reply_html("❌ এই কেস আইডি পাওয়া যায়নি।")
        return

    # ফোন বা ইউজারনেম
    if is_phone_number(text):
        results = search_by_phone(text)
        search_type = "ফোন নম্বর"
    else:
        results = search_by_username(text)
        search_type = "ইউজারনেম"

    if not results:
        await update.message.reply_html(f"❌ এই {search_type} দিয়ে কোনো কেস পাওয়া যায়নি।")
        return

    if len(results) == 1:
        await update.message.reply_html(format_single_case(results[0]))
    else:
        limit = 5
        parts = [format_single_case(c) for c in results[:limit]]
        text = "\n\n---\n\n".join(parts)
        if len(results) > limit:
            text += f"\n\n<i>... এবং আরও {len(results) - limit} টি কেস আছে। আরও নির্দিষ্ট তথ্য দিন।</i>"
        await update.message.reply_html(text)

# ------------------------ এরর হ্যান্ডলার ------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(msg="আপডেট প্রসেস করতে সমস্যা:", exc_info=context.error)
    if update and isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("⚠️ একটি সমস্যা হয়েছে, পরে চেষ্টা করুন।")

# ------------------------ মেইন ------------------------
def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # অথেন্টিকেশন কনভারসেশন
    auth_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start_command)],
        states={
            ASK_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_email)],
            ASK_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password)],
        },
        fallbacks=[CommandHandler("cancel", cancel_auth)],
    )
    application.add_handler(auth_conv)

    # সাধারণ মেসেজ (সার্চ)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)

    # Python 3.14+ ইভেন্ট লুপ
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        logger.info("বট চলছে...")
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        loop.close()

if __name__ == "__main__":
    main()
