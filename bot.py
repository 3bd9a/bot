import asyncio
import json
import logging
import os
import time
from datetime import timedelta

import aiohttp
import redis.asyncio as aioredis
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ==================== Config ====================
class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
    API_URL = os.getenv("API_URL", "https://painel.meowssh.shop:5000/test_ssh_public")

    COOLDOWN_SECONDS = 3 * 60 * 60  # 3 ساعات
    REQUEST_TIMEOUT = 15
    MAX_CONCURRENT_REQUESTS = 15  # تقليل الضغط على API

# ==================== Logging ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("ssh_bot")

# ==================== Globals ====================
redis_client: aioredis.Redis | None = None
semaphore: asyncio.Semaphore | None = None
COOLDOWNS_ZSET = "cooldowns_zset"
USERS_ZSET = "users_zset"

# ==================== Helpers ====================
def format_timedelta_seconds(s: int) -> str:
    td = timedelta(seconds=int(s))
    days, hours, rem = td.days, td.seconds // 3600, td.seconds % 3600
    minutes, seconds = divmod(rem, 60)
    parts = []
    if days: parts.append(f"{days}d")
    if hours: parts.append(f"{hours}h")
    if minutes: parts.append(f"{minutes}m")
    if seconds and not parts: parts.append(f"{seconds}s")
    return " ".join(parts) if parts else "0s"

def escape_markdown_v2(text: str) -> str:
    escape_chars = r"_*[]()~`>#+-=|{}.!$"
    return "".join(f"\\{c}" if c in escape_chars else c for c in text)

async def set_user_cooldown(user_id: int):
    now_ts = int(time.time())
    expiry = now_ts + Config.COOLDOWN_SECONDS
    await redis_client.set(f"cooldown:{user_id}", expiry)
    await redis_client.zadd(COOLDOWNS_ZSET, {str(user_id): expiry})

async def get_user_cooldown_remaining(user_id: int) -> int:
    v = await redis_client.get(f"cooldown:{user_id}")
    if not v: return 0
    rem = int(v) - int(time.time())
    return rem if rem > 0 else 0

def get_main_keyboard():
    kb = [
        [InlineKeyboardButton("🔐 احصل على حساب SSH", callback_data="get_account")],
        [InlineKeyboardButton("📊 إحصائياتي", callback_data="mystats")],
        [InlineKeyboardButton("❓ المساعدة", callback_data="help")]
    ]
    return InlineKeyboardMarkup(kb)

# ==================== API Call ====================
async def _call_api_create_account(user_id: int, username: str):
    payload = {"store_owner_id": 1}
    timeout = aiohttp.ClientTimeout(total=Config.REQUEST_TIMEOUT)
    
    logger.info(f"🔗 API Request: {Config.API_URL}")
    logger.info(f"📤 Payload: {payload}")
    
    async with semaphore:
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    Config.API_URL,
                    json=payload,
                    headers={
                        'Accept': 'application/json',
                        'User-Agent': 'SSH-Bot/1.0'
                    }
                ) as resp:
                    response_text = await resp.text()
                    logger.info(f"📥 Response Status: {resp.status}")
                    logger.info(f"📥 Response Body: {response_text}")
                    
                    if resp.status in (200, 201):
                        try:
                            data = json.loads(response_text)
                            logger.info(f"✅ Parsed JSON successfully: {data}")
                            return data
                        except json.JSONDecodeError as e:
                            logger.error(f"❌ JSON decode error: {e}")
                            raise RuntimeError(f"Invalid JSON response: {response_text}")
                    else:
                        logger.error(f"❌ API error {resp.status}: {response_text}")
                        raise RuntimeError(f"API returned {resp.status}: {response_text}")
                        
        except asyncio.TimeoutError:
            logger.error("⏰ Request timeout")
            raise RuntimeError("Request timeout - server took too long to respond")
        except aiohttp.ClientError as e:
            logger.error(f"🌐 Network error: {e}")
            raise RuntimeError(f"Network connection failed: {str(e)}")
        except Exception as e:
            logger.error(f"💥 Unexpected error: {e}")
            raise RuntimeError(f"Unexpected error: {str(e)}")

async def provide_account_for_user(user_id: int, username: str):
    data = await _call_api_create_account(user_id, username)
    await set_user_cooldown(user_id)

    # تحديث إحصاءات المستخدم وتسجيله في USERS_ZSET
    current_count = int(await redis_client.get(f"stats:user_requests:{user_id}") or 0)
    await redis_client.set(f"stats:user_requests:{user_id}", current_count + 1)
    await redis_client.zadd(USERS_ZSET, {str(user_id): int(time.time())})
    
    logger.info(f"✅ Account created for user {user_id}, total requests: {current_count + 1}")
    return data

async def send_account_message(chat_id: int, data: dict, context: ContextTypes.DEFAULT_TYPE):
    usuario = data.get('Usuario', 'N/A')
    senha = data.get('Senha', 'N/A')  
    expiracao = data.get('Expiracao', '3 ساعات')
    ip = data.get('IP', 'N/A')
    limite = data.get('limite', 'N/A')
    
    ssh_info = (
        f"🎉 **تم إنشاء حساب SSH بنجاح!**\n\n"
        f"👤 **المستخدم:** `{escape_markdown_v2(usuario)}`\n"
        f"🔑 **كلمة المرور:** `{escape_markdown_v2(senha)}`\n"
        f"🌐 **الخادم:** `{escape_markdown_v2(ip)}`\n"
        f"👥 **عدد الاتصالات:** `{escape_markdown_v2(str(limite))}`\n"
        f"⏰ **مدة الصلاحية:** {escape_markdown_v2(expiracao)}\n\n"
        f"📋 **طريقة الاتصال:**\n"
        f"`ssh {escape_markdown_v2(usuario)}@{escape_markdown_v2(ip)}`\n\n"
        f"⏰ يمكنك طلب حساب جديد بعد 3 ساعات."
    )
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 حساب جديد (بعد 3 ساعات)", callback_data="get_account")],
        [InlineKeyboardButton("📊 إحصائياتي", callback_data="mystats")]
    ])
    
    await context.bot.send_message(
        chat_id=chat_id, 
        text=ssh_info, 
        parse_mode="Markdown",
        reply_markup=keyboard
    )

# ==================== Handlers ====================
async def handle_get_request(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, username: str, chat_id: int):
    rem = await get_user_cooldown_remaining(user_id)
    if rem:
        await context.bot.send_message(
            chat_id=chat_id, 
            text=f"⏳ **لديك حساب نشط!**\n\nيمكنك طلب حساب جديد بعد: **{format_timedelta_seconds(rem)}**",
            parse_mode="Markdown"
        )
        return
        
    progress = await context.bot.send_message(chat_id=chat_id, text="⏳ جاري إنشاء الحساب...")
    
    try:
        data = await provide_account_for_user(user_id, username)
        await send_account_message(chat_id, data, context)
        await progress.delete()
    except Exception as e:
        logger.exception(f"Failed to create account for user {user_id}")
        error_msg = "❌ **فشل إنشاء الحساب**\n\n"
        if "timeout" in str(e).lower():
            error_msg += "⏰ انتهت مهلة الاتصال. حاول مرة أخرى."
        elif "network" in str(e).lower():
            error_msg += "🌐 مشكلة في الاتصال. تحقق من الإنترنت."
        elif "400" in str(e):
            error_msg += "⚙️ مشكلة في إعدادات الخادم. حاول لاحقاً."
        else:
            error_msg += f"🔧 خطأ تقني: حاول لاحقاً."
        await progress.edit_text(error_msg, parse_mode="Markdown")

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    rem = await get_user_cooldown_remaining(user.id)
    rem_text = format_timedelta_seconds(rem) if rem else "جاهز الآن ✅"
    
    welcome_msg = (
        f"🔐 **بوت حسابات SSH المجانية**\n\n"
        f"مرحباً {escape_markdown_v2(user.first_name)}\n\n"
        f"• **حالتك:** {rem_text}\n"
        f"• **مدة الحساب:** 3 ساعات\n"
        f"• **مجاني تماماً**"
    )
    
    await update.message.reply_text(
        welcome_msg,
        reply_markup=get_main_keyboard(),
        parse_mode="MarkdownV2"
    )

async def get_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await handle_get_request(update, context, user.id, user.username or str(user.id), update.effective_chat.id)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📖 **مساعدة بوت SSH**\n\n"
        "**الأوامر المتاحة:**\n"
        "• `/start` - البداية\n"
        "• `/get` - احصل على حساب\n"
        "• `/mystats` - إحصائياتك\n"
        "• `/help` - هذه المساعدة\n\n"
        "**معلومات مهمة:**\n"
        "• الحسابات مجانية تماماً\n"
        "• مدة الصلاحية: 3 ساعات\n"
        "• فترة الانتظار: 3 ساعات\n"
        "• يمكن اتصال واحد فقط لكل حساب"
    )
    
    await context.bot.send_message(chat_id=update.effective_chat.id, text=help_text, parse_mode="Markdown")

async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    
    if query.data == "get_account":
        await handle_get_request(update, context, user.id, user.username or str(user.id), query.message.chat.id)
    elif query.data == "mystats":
        await my_stats_command(update, context)
    elif query.data == "help":
        await help_command(update, context)

async def my_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_requests = int(await redis_client.get(f"stats:user_requests:{user.id}") or 0)
    rem = await get_user_cooldown_remaining(user.id)
    rem_text = format_timedelta_seconds(rem) if rem else "جاهز الآن ✅"
    
    stats_text = (
        f"📊 **إحصائيات {escape_markdown_v2(user.first_name)}**\n\n"
        f"• **إجمالي الطلبات:** {user_requests}\n"
        f"• **الحالة الحالية:** {rem_text}\n"
        f"• **آخر نشاط:** الآن"
    )
    
    await context.bot.send_message(chat_id=update.effective_chat.id, text=stats_text, parse_mode="Markdown")

# ==================== Notifier ====================
async def cooldown_notifier_task(app: Application):
    logger.info("🔔 Notifier task started")
    while True:
        try:
            now = int(time.time())
            expired = await redis_client.zrangebyscore(COOLDOWNS_ZSET, 0, now)
            
            for uid_str in expired:
                try:
                    uid = int(uid_str)
                    await redis_client.delete(f"cooldown:{uid}")
                    
                    kb = InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔐 احصل على حساب جديد", callback_data="get_account")
                    ]])
                    
                    await app.bot.send_message(
                        chat_id=uid, 
                        text="⏰ **انتهت فترة الانتظار!**\n\nيمكنك الآن طلب حساب SSH جديد.",
                        reply_markup=kb,
                        parse_mode="Markdown"
                    )
                    logger.info(f"🔔 Notified user {uid}")
                    
                except Exception as e:
                    logger.warning(f"Notifier failed for {uid_str}: {e}")
                finally:
                    await redis_client.zrem(COOLDOWNS_ZSET, uid_str)
                    
            await asyncio.sleep(30)
            
        except asyncio.CancelledError:
            logger.info("🔔 Notifier task cancelled")
            break
        except Exception:
            logger.exception("Error in notifier task")
            await asyncio.sleep(5)

# ==================== Web ====================
async def health_handler(request):
    return web.json_response({"status": "healthy", "timestamp": time.time(), "service": "ssh-bot"})

async def stats_handler(request):
    try:
        total_users = await redis_client.zcard(USERS_ZSET)
        return web.json_response({"total_users": total_users, "timestamp": time.time()})
    except Exception:
        return web.json_response({"error": "stats unavailable"}, status=500)

# ==================== Main ====================
async def main():
    global redis_client, semaphore
    
    if not Config.BOT_TOKEN:
        raise ValueError("❌ BOT_TOKEN environment variable is required")
    
    logger.info(f"🔧 Config loaded - API: {Config.API_URL}")
    
    redis_client = aioredis.from_url(Config.REDIS_URL, decode_responses=True)
    await redis_client.ping()
    logger.info("✅ Connected to Redis")

    semaphore = asyncio.Semaphore(Config.MAX_CONCURRENT_REQUESTS)

    app = Application.builder().token(Config.BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("get", get_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("mystats", my_stats_command))
    app.add_handler(CallbackQueryHandler(callback_query_handler))

    notifier_task = asyncio.create_task(cooldown_notifier_task(app))

    web_app = web.Application()
    web_app.add_routes([
        web.get("/", health_handler), 
        web.get("/health", health_handler),
        web.get("/stats", stats_handler)
    ])
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8000)
    await site.start()
    logger.info("🌐 Health server started on port 8000")

    try:
        logger.info("🤖 Starting bot...")
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        logger.info("✅ Bot is running!")
        
        while True:
            await asyncio.sleep(1)
            
    except KeyboardInterrupt:
        logger.info("⏹️ Received interrupt signal")
    finally:
        logger.info("🛑 Shutting down...")
        notifier_task.cancel()
        try: await notifier_task
        except asyncio.CancelledError: pass
        
        await app.updater.stop()
        await app.stop()  
        await app.shutdown()
        await runner.cleanup()
        await redis_client.close()
        logger.info("✅ Cleanup completed")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Bot failed to start: {e}")
        raise
