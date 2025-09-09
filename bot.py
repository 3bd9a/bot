# bot_final.py
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Dict, Optional

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

# ========== Config ==========
class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
    API_URL = os.getenv("API_URL", "https://painel.meowssh.shop:5000/test_ssh_public")

    COOLDOWN_SECONDS = 3 * 60 * 60  # 3 hours
    REQUEST_TIMEOUT = 15
    MAX_CONCURRENT_REQUESTS = 25

    ADMIN_USER_IDS = set(map(int, os.getenv("ADMIN_USERS", "").split(","))) if os.getenv("ADMIN_USERS") else set()

# ========== Logging ==========
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

# ========== Redis keys ==========
COOLDOWNS_ZSET = "cooldowns_zset"

# ========== Helpers ==========
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

# ========== Redis helpers (use app.bot_data['redis']) ==========
async def set_user_cooldown(redis_client: aioredis.Redis, user_id: int):
    now = int(time.time())
    expiry = now + Config.COOLDOWN_SECONDS
    await redis_client.set(f"cooldown:{user_id}", expiry)
    await redis_client.zadd(COOLDOWNS_ZSET, {str(user_id): expiry})

async def get_user_cooldown_remaining(redis_client: aioredis.Redis, user_id: int) -> int:
    v = await redis_client.get(f"cooldown:{user_id}")
    if not v:
        return 0
    expiry = int(v)
    now = int(time.time())
    rem = expiry - now
    return rem if rem > 0 else 0

async def remove_user_cooldown(redis_client: aioredis.Redis, user_id: int):
    await redis_client.delete(f"cooldown:{user_id}")
    await redis_client.zrem(COOLDOWNS_ZSET, str(user_id))

async def log_request(redis_client: aioredis.Redis, user_id: int, command: str):
    pipe = redis_client.pipeline()
    pipe.incr("stats:total_requests")
    pipe.incr(f"stats:user_requests:{user_id}")
    pipe.incr(f"stats:commands:{command}")
    pipe.sadd("stats:unique_users", user_id)
    await pipe.execute()

async def log_success(redis_client: aioredis.Redis):
    await redis_client.incr("stats:success_count")

async def log_error(redis_client: aioredis.Redis, error_type: str = "general"):
    pipe = redis_client.pipeline()
    pipe.incr("stats:error_count")
    pipe.incr(f"stats:errors:{error_type}")
    await pipe.execute()

async def get_stats(redis_client: aioredis.Redis) -> Dict:
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

# ========== UI ==========
def main_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("🔐 احصل على حساب SSH", callback_data="get_account")],
        [InlineKeyboardButton("📊 إحصائياتي", callback_data="mystats"),
         InlineKeyboardButton("ℹ️ مساعدة", callback_data="help")],
    ]
    return InlineKeyboardMarkup(kb)

# ========== API call ==========
async def _call_api_create_account(user_id: int, username: str, semaphore: asyncio.Semaphore) -> Dict:
    payload = {"store_owner_id": 1, "user_id": user_id, "username": username, "timestamp": int(time.time())}
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

# ========== Handlers ==========
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    redis_client: aioredis.Redis = context.application.bot_data["redis"]
    await log_request(redis_client, update.effective_user.id, "start")
    rem = await get_user_cooldown_remaining(redis_client, update.effective_user.id)
    rem_text = format_timedelta_seconds(rem) if rem else "جاهز الآن"
    text = f"🔐 بوت حسابات SSH\n\nمرحباً @{update.effective_user.username or update.effective_user.id}\n\n• حالة الحساب: {rem_text}"
    await update.message.reply_text(text, reply_markup=main_keyboard())

async def handle_get_request(user_id: int, username: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    redis_client: aioredis.Redis = context.application.bot_data["redis"]
    semaphore: asyncio.Semaphore = context.application.bot_data["semaphore"]
    await log_request(redis_client, user_id, "get")
    rem = await get_user_cooldown_remaining(redis_client, user_id)
    if rem:
        await context.bot.send_message(chat_id=chat_id, text=f"⏳ لديك حساب بالفعل. يمكنك المحاولة بعد: {format_timedelta_seconds(rem)}")
        return
    progress = await context.bot.send_message(chat_id=chat_id, text="⏳ جاري إنشاء الحساب...")
    try:
        data = await _call_api_create_account(user_id, username, semaphore)
        await set_user_cooldown(redis_client, user_id)
        await log_success(redis_client)
        msg = (
            f"🎉 **تم إنشاء حساب SSH بنجاح!**\n\n"
            f"👤 المستخدم: `{data.get('Usuario','N/A')}`\n"
            f"🔑 كلمة المرور: `{data.get('Senha','N/A')}`\n"
            f"⏰ مدة الصلاحية: {data.get('Expiracao','3 ساعات')}\n\n"
            "⏰ بعد 3 ساعات يمكنك طلب حساب جديد."
        )
        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
        await progress.delete()
    except Exception as e:
        await log_error(redis_client, "api_error")
        logger.exception("API/create account failed")
        await progress.edit_text("❌ فشل إنشاء الحساب. حاول لاحقًا.")

async def get_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await handle_get_request(user.id, user.username or str(user.id), update.effective_chat.id, context)

async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = update.effective_user
    if data == "get_account":
        await handle_get_request(user.id, user.username or str(user.id), query.message.chat.id, context)
    elif data == "mystats":
        await my_stats_command(update, context)
    elif data == "help":
        await help_handler(update, context)

async def my_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # works for both command and callback (update may be callback)
    redis_client: aioredis.Redis = context.application.bot_data["redis"]
    if update.message:
        chat_id = update.message.chat_id
        user = update.effective_user
    else:
        chat_id = update.callback_query.message.chat_id
        user = update.effective_user
    user_requests = int(await redis_client.get(f"stats:user_requests:{user.id}") or 0)
    rem = await get_user_cooldown_remaining(redis_client, user.id)
    rem_text = format_timedelta_seconds(rem) if rem else "جاهز الآن"
    text = f"📊 إحصائياتك\n\n• إجمالي الطلبات: {user_requests}\n• حالة الحساب: {rem_text}"
    await context.bot.send_message(chat_id=chat_id, text=text)

async def admin_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in Config.ADMIN_USER_IDS:
        await update.message.reply_text("❌ هذا الأمر للمشرفين فقط")
        return
    redis_client: aioredis.Redis = context.application.bot_data["redis"]
    s = await get_stats(redis_client)
    text = (
        f"🔧 إحصائيات عامة\n\n"
        f"• إجمالي الطلبات: {s['total_requests']}\n"
        f"• نجاحات: {s['success_count']}\n"
        f"• أخطاء: {s['error_count']}\n"
        f"• مستخدمين فريدين: {s['unique_users']}\n"
    )
    await update.message.reply_text(text)

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "📚 **مساعدة البوت**\n\n"
        "/get - الحصول على حساب SSH\n"
        "/mystats - عرض إحصائياتك\n"
        "كل مستخدم يمكنه الحصول على حساب واحد كل 3 ساعات."
    )
    # callback or message
    if update.message:
        await update.message.reply_text(txt, parse_mode="Markdown")
    else:
        await update.callback_query.message.reply_text(txt, parse_mode="Markdown")

# ========== Notifier (as JobQueue job) ==========
async def notifier_job(context: ContextTypes.DEFAULT_TYPE):
    redis_client: aioredis.Redis = context.application.bot_data["redis"]
    bot = context.application.bot
    try:
        now = int(time.time())
        expired = await redis_client.zrangebyscore(COOLDOWNS_ZSET, 0, now)
        if not expired:
            return
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
                await bot.send_message(chat_id=uid, text="⏰ انتهت فترة الثلاث ساعات. يمكنك الآن طلب حساب جديد.", reply_markup=kb)
            except Exception as e:
                logger.warning(f"Notifier failed for {uid}: {e}")
    except Exception:
        logger.exception("Notifier job error")

# ========== Web health ==========
async def health_handler(request):
    redis_client: aioredis.Redis = request.app["redis"]
    s = await get_stats(redis_client)
    return web.json_response({"status": "ok", "stats": s, "ts": datetime.utcnow().isoformat() + "Z"})

# ========== Main (synchronous) ==========
def main():
    if not Config.BOT_TOKEN:
        logger.critical("BOT_TOKEN not set in environment")
        return

    # create redis client (no await)
    redis_client = aioredis.from_url(Config.REDIS_URL, decode_responses=True)

    # verify connection using event loop (safe here)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(redis_client.ping())
    except Exception:
        logger.exception("Failed connect to Redis")
        return
    finally:
        loop.close()

    logger.info("Connected to Redis")

    # semaphore shared object
    semaphore = asyncio.Semaphore(Config.MAX_CONCURRENT_REQUESTS)

    # build application
    app = Application.builder().token(Config.BOT_TOKEN).build()

    # store shared resources
    app.bot_data["redis"] = redis_client
    app.bot_data["semaphore"] = semaphore

    # add handlers
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("get", get_command))
    app.add_handler(CommandHandler("mystats", my_stats_command))
    app.add_handler(CommandHandler("admin", admin_stats_command))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CallbackQueryHandler(callback_query_handler))

    # schedule notifier job (using job queue)
    # first=10 gives first run after 10s, interval=30s
    app.job_queue.run_repeating(notifier_job, interval=30, first=10)

    # start lightweight web health server on port 8000 in background
    # we attach the same redis client for health checks
    web_app = web.Application()
    web_app["redis"] = redis_client
    web_app.router.add_get("/", health_handler)
    web_app.router.add_get("/health", health_handler)
    runner = web.AppRunner(web_app)
    # run web server in separate loop thread to avoid blocking run_polling
    async def start_web():
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", 8000)
        await site.start()
        logger.info("Health endpoint started on :8000")

    # run the aiohttp web server in a task inside the application's loop via post_init
    async def _on_startup(app_local: Application):
        # start web within same loop
        await start_web()

    app.post_init(_on_startup)

    # finally run polling (blocks)
    logger.info("Starting bot (run_polling)...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
