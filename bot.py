import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Dict

import aiohttp
import redis.asyncio as aioredis
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ==================== Configuration ====================
class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    # مهم: لازم تحط هذا في Secrets بـ Koyeb
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
    API_URL = os.getenv(
        "API_URL", "https://painel.meowssh.shop:5000/test_ssh_public"
    )

    COOLDOWN_SECONDS = 3 * 60 * 60  # 3 ساعات
    MAX_REQUESTS_PER_HOUR = 10
    MAX_REQUESTS_PER_DAY = 50
    REQUEST_TIMEOUT = 15
    MAX_CONCURRENT_REQUESTS = 25

    ADMIN_USER_IDS = set(
        map(int, os.getenv("ADMIN_USERS", "").split(","))) if os.getenv("ADMIN_USERS") else set()

# ==================== Logging ====================
from logging.handlers import RotatingFileHandler

logger = logging.getLogger("ssh_bot")
logger.setLevel(logging.INFO)
handler = RotatingFileHandler("bot.log", maxBytes=10_000_000, backupCount=3, encoding="utf-8")
fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
handler.setFormatter(fmt)
logger.addHandler(handler)

console = logging.StreamHandler()
console.setFormatter(fmt)
logger.addHandler(console)

# ==================== Globals ====================
redis_client: aioredis.Redis | None = None
semaphore: asyncio.Semaphore | None = None

COOLDOWNS_ZSET = "cooldowns_zset"

# ==================== Helpers ====================
def format_timedelta_seconds(s: int) -> str:
    td = timedelta(seconds=int(s))
    days = td.days
    hours, rem = divmod(td.seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds and not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts) if parts else "0s"

async def set_user_cooldown(user_id: int, now_ts: int):
    expiry = now_ts + Config.COOLDOWN_SECONDS
    await redis_client.set(f"cooldown:{user_id}", expiry)
    await redis_client.zadd(COOLDOWNS_ZSET, {str(user_id): expiry})

async def get_user_cooldown_remaining(user_id: int) -> int:
    v = await redis_client.get(f"cooldown:{user_id}")
    if not v:
        return 0
    expiry = int(v)
    now = int(time.time())
    rem = expiry - now
    return rem if rem > 0 else 0

async def remove_user_cooldown(user_id: int):
    await redis_client.delete(f"cooldown:{user_id}")
    await redis_client.zrem(COOLDOWNS_ZSET, str(user_id))

async def log_request(user_id: int, command: str):
    pipe = redis_client.pipeline()
    pipe.incr("stats:total_requests")
    pipe.incr(f"stats:user_requests:{user_id}")
    pipe.incr(f"stats:commands:{command}")
    pipe.sadd("stats:unique_users", user_id)
    await pipe.execute()

async def log_success():
    await redis_client.incr("stats:success_count")

async def log_error(error_type: str = "general"):
    pipe = redis_client.pipeline()
    pipe.incr("stats:error_count")
    pipe.incr(f"stats:errors:{error_type}")
    await pipe.execute()

async def get_stats() -> Dict:
    pipe = redis_client.pipeline()
    pipe.get("stats:total_requests")
    pipe.get("stats:success_count")
    pipe.get("stats:error_count")
    pipe.scard("stats:unique_users")
    results = await pipe.execute()
    return {
        "total_requests": int(results[0] or 0),
        "success_count": int(results[1] or 0),
        "error_count": int(results[2] or 0),
        "unique_users": int(results[3] or 0),
    }

# ==================== Bot UI ====================
def get_main_keyboard():
    kb = [
        [InlineKeyboardButton("🔐 احصل على حساب SSH", callback_data="get_account")],
        [InlineKeyboardButton("📊 إحصائياتي", callback_data="mystats")],
    ]
    return InlineKeyboardMarkup(kb)

# ==================== Handlers ====================
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    rem = await get_user_cooldown_remaining(user.id)
    rem_text = format_timedelta_seconds(rem) if rem else "جاهز الآن"
    await log_request(user.id, "start")
    await update.message.reply_text(
        f"🔐 بوت حسابات SSH\n\nمرحباً @{user.username or user.id}\n\n• لديك: {rem_text}",
        reply_markup=get_main_keyboard(),
    )

async def _call_api_create_account(user_id: int, username: str) -> Dict:
    payload = {"user_id": user_id, "username": username, "timestamp": int(time.time())}
    timeout = aiohttp.ClientTimeout(total=Config.REQUEST_TIMEOUT)
    async with semaphore:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(Config.API_URL, json=payload) as resp:
                text = await resp.text()
                if resp.status in (200, 201):
                    try:
                        return await resp.json()
                    except Exception:
                        return json.loads(text)
                raise RuntimeError(f"API error {resp.status}: {text}")

async def provide_account_for_user(user_id: int, username: str):
    data = await _call_api_create_account(user_id, username)
    now_ts = int(time.time())
    await set_user_cooldown(user_id, now_ts)
    await log_success()
    return data

async def send_account_message(chat_id: int, data: Dict, context: ContextTypes.DEFAULT_TYPE):
    ssh_info = (
        f"🎉 **تم إنشاء حساب SSH بنجاح!**\n\n"
        f"👤 المستخدم: `{data.get('Usuario','N/A')}`\n"
        f"🔑 كلمة المرور: `{data.get('Senha','N/A')}`\n"
        f"⏰ مدة الصلاحية: {data.get('Expiracao','3 ساعات')}\n\n"
        f"⏰ بعد 3 ساعات يمكنك طلب حساب جديد."
    )
    await context.bot.send_message(chat_id=chat_id, text=ssh_info, parse_mode="Markdown")

async def handle_get_request(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, username: str, chat_id: int):
    await log_request(user_id, "get")
    rem = await get_user_cooldown_remaining(user_id)
    if rem:
        await context.bot.send_message(chat_id=chat_id, text=f"⏳ لديك حساب بالفعل. المحاولة بعد: {format_timedelta_seconds(rem)}")
        return
    progress = await context.bot.send_message(chat_id=chat_id, text="⏳ جاري إنشاء الحساب...")
    try:
        data = await provide_account_for_user(user_id, username)
        await send_account_message(chat_id, data, context)
        await progress.delete()
    except Exception:
        await log_error("api_error")
        logger.exception("Failed to create account")
        await progress.edit_text("❌ فشل إنشاء الحساب. حاول لاحقًا.")

async def get_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await handle_get_request(update, context, user.id, user.username or str(user.id), update.effective_chat.id)

async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if query.data == "get_account":
        await handle_get_request(update, context, user.id, user.username or str(user.id), query.message.chat.id)
    elif query.data == "mystats":
        await my_stats_command(update, context)

async def my_stats_command(update: Update | None, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_requests = int(await redis_client.get(f"stats:user_requests:{user.id}") or 0)
    rem = await get_user_cooldown_remaining(user.id)
    rem_text = format_timedelta_seconds(rem) if rem else "جاهز الآن"
    text = f"📊 إحصائياتك\n\n• إجمالي الطلبات: {user_requests}\n• حالة الحساب: {rem_text}"
    await context.bot.send_message(chat_id=update.effective_chat.id, text=text)

async def admin_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in Config.ADMIN_USER_IDS:
        await update.message.reply_text("❌ هذا الأمر للمشرفين فقط")
        return
    s = await get_stats()
    text = (
        f"🔧 إحصائيات عامة\n\n"
        f"• إجمالي الطلبات: {s['total_requests']}\n"
        f"• نجاحات: {s['success_count']}\n"
        f"• أخطاء: {s['error_count']}\n"
        f"• مستخدمين فريدين: {s['unique_users']}\n"
    )
    await update.message.reply_text(text)

# ==================== Notifier ====================
async def cooldown_notifier_task(app: Application):
    logger.info("Notifier task started")
    while True:
        try:
            now = int(time.time())
            expired = await redis_client.zrangebyscore(COOLDOWNS_ZSET, 0, now)
            if expired:
                for uid_str in expired:
                    try:
                        uid = int(uid_str)
                    except Exception:
                        await redis_client.zrem(COOLDOWNS_ZSET, uid_str)
                        continue
                    await redis_client.zrem(COOLDOWNS_ZSET, uid_str)
                    await redis_client.delete(f"cooldown:{uid}")
                    try:
                        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔐 احصل على حساب جديد", callback_data="get_account")]])
                        await app.bot.send_message(chat_id=uid, text="⏰ انتهت فترة الثلاث ساعات. يمكنك الآن طلب حساب جديد.", reply_markup=kb)
                    except Exception as e:
                        logger.warning(f"Failed to notify user {uid}: {e}")
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Error in notifier task")
            await asyncio.sleep(5)

# ==================== Web health ====================
async def health_handler(request):
    s = await get_stats()
    return web.json_response({"status": "ok", "stats": s, "ts": datetime.utcnow().isoformat() + "Z"})

# ==================== Main ====================
async def main():
    global redis_client, semaphore

    if not Config.BOT_TOKEN:
        logger.critical("BOT_TOKEN not set in environment")
        return

    redis_client = aioredis.from_url(Config.REDIS_URL, decode_responses=True)
    try:
        await redis_client.ping()
        logger.info("Connected to Redis")
    except Exception:
        logger.exception("Failed connect to Redis")
        return

    semaphore = asyncio.Semaphore(Config.MAX_CONCURRENT_REQUESTS)

    app = Application.builder().token(Config.BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("get", get_command))
    app.add_handler(CommandHandler("mystats", my_stats_command))
    app.add_handler(CommandHandler("admin", admin_stats_command))
    app.add_handler(CallbackQueryHandler(callback_query_handler))

    web_app = web.Application()
    web_app.add_routes([web.get("/", health_handler), web.get("/health", health_handler)])
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8000)
    await site.start()

    notifier_task = asyncio.create_task(cooldown_notifier_task(app))

    try:
        logger.info("Starting bot polling")
        await app.run_polling(drop_pending_updates=True, close_loop=False)
    finally:
        notifier_task.cancel()
        await notifier_task
        await runner.cleanup()
        await redis_client.close()

if __name__ == "__main__":
    asyncio.run(main())
