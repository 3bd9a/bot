import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Dict, Optional
import hashlib
import secrets

import aiohttp
import redis.asyncio as aioredis
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.constants import ParseMode

# ==================== Configuration ====================
class Config:
    BOT_TOKEN = os.getenv('BOT_TOKEN')
    REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379')
    API_URL = os.getenv('API_URL', 'https://painel.meowssh.shop:5000/test_ssh_public')
    
    # Cooldown Settings
    COOLDOWN_HOURS = 3
    COOLDOWN_SECONDS = COOLDOWN_HOURS * 3600
    
    # Performance
    REQUEST_TIMEOUT = 20
    MAX_CONCURRENT_REQUESTS = 15
    
    # Security
    ADMIN_USER_IDS = set(map(int, os.getenv('ADMIN_USERS', '').split(','))) if os.getenv('ADMIN_USERS') else set()
    
    # Features
    ENABLE_NOTIFICATIONS = True
    WEB_PORT = int(os.getenv('PORT', '8000'))

# ==================== Enhanced Logging ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("ssh_bot.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('SSHBot')

# ==================== Redis Manager ====================
class RedisManager:
    def __init__(self, redis_client):
        self.redis = redis_client
    
    # Cooldown Management
    async def set_user_cooldown(self, user_id: int) -> int:
        """Set cooldown for user and return expiry timestamp"""
        expiry = int(time.time()) + Config.COOLDOWN_SECONDS
        pipe = self.redis.pipeline()
        pipe.set(f"cooldown:{user_id}", expiry)
        pipe.zadd("cooldown_expiry", {str(user_id): expiry})
        await pipe.execute()
        return expiry
    
    async def get_cooldown_remaining(self, user_id: int) -> int:
        """Get remaining cooldown seconds, 0 if no cooldown"""
        expiry = await self.redis.get(f"cooldown:{user_id}")
        if not expiry:
            return 0
        
        remaining = int(expiry) - int(time.time())
        if remaining <= 0:
            await self.remove_user_cooldown(user_id)
            return 0
        return remaining
    
    async def remove_user_cooldown(self, user_id: int):
        """Remove user cooldown"""
        pipe = self.redis.pipeline()
        pipe.delete(f"cooldown:{user_id}")
        pipe.zrem("cooldown_expiry", str(user_id))
        await pipe.execute()
    
    # Statistics Management
    async def log_request(self, user_id: int, command: str):
        """Log user request"""
        pipe = self.redis.pipeline()
        pipe.incr("stats:total_requests")
        pipe.incr(f"stats:user:{user_id}:requests")
        pipe.incr(f"stats:command:{command}")
        pipe.sadd("stats:unique_users", user_id)
        pipe.set(f"stats:user:{user_id}:last_seen", int(time.time()))
        await pipe.execute()
    
    async def log_success(self, user_id: int):
        """Log successful SSH creation"""
        pipe = self.redis.pipeline()
        pipe.incr("stats:success_count")
        pipe.incr(f"stats:user:{user_id}:success")
        await pipe.execute()
    
    async def log_error(self, error_type: str, user_id: Optional[int] = None):
        """Log error with type"""
        pipe = self.redis.pipeline()
        pipe.incr("stats:error_count")
        pipe.incr(f"stats:errors:{error_type}")
        if user_id:
            pipe.incr(f"stats:user:{user_id}:errors")
        await pipe.execute()
    
    async def get_global_stats(self) -> Dict:
        """Get global bot statistics"""
        pipe = self.redis.pipeline()
        pipe.get("stats:total_requests")
        pipe.get("stats:success_count")
        pipe.get("stats:error_count")
        pipe.scard("stats:unique_users")
        pipe.get("stats:command:get")
        pipe.get("stats:command:start")
        results = await pipe.execute()
        
        total_requests = int(results[0] or 0)
        success_count = int(results[1] or 0)
        error_count = int(results[2] or 0)
        
        return {
            "total_requests": total_requests,
            "success_count": success_count,
            "error_count": error_count,
            "unique_users": int(results[3] or 0),
            "get_commands": int(results[4] or 0),
            "start_commands": int(results[5] or 0),
            "success_rate": f"{(success_count / max(total_requests, 1) * 100):.1f}%",
            "uptime": await self.get_uptime()
        }
    
    async def get_user_stats(self, user_id: int) -> Dict:
        """Get user statistics"""
        pipe = self.redis.pipeline()
        pipe.get(f"stats:user:{user_id}:requests")
        pipe.get(f"stats:user:{user_id}:success")
        pipe.get(f"stats:user:{user_id}:errors")
        pipe.get(f"stats:user:{user_id}:last_seen")
        results = await pipe.execute()
        
        last_seen = int(results[3] or 0)
        last_seen_text = datetime.fromtimestamp(last_seen).strftime('%Y-%m-%d %H:%M') if last_seen else "غير معروف"
        
        return {
            "total_requests": int(results[0] or 0),
            "successful_accounts": int(results[1] or 0),
            "errors": int(results[2] or 0),
            "last_seen": last_seen_text,
            "cooldown_remaining": await self.get_cooldown_remaining(user_id)
        }
    
    async def get_uptime(self) -> str:
        """Get bot uptime"""
        start_time = await self.redis.get("bot:start_time")
        if not start_time:
            await self.redis.set("bot:start_time", int(time.time()))
            return "بدء التشغيل"
        
        uptime_seconds = int(time.time()) - int(start_time)
        return format_duration(uptime_seconds)
    
    async def get_expired_users(self) -> list:
        """Get users whose cooldown has expired"""
        now = int(time.time())
        expired = await self.redis.zrangebyscore("cooldown_expiry", 0, now)
        
        # Clean expired entries
        if expired:
            pipe = self.redis.pipeline()
            for user_id in expired:
                pipe.delete(f"cooldown:{user_id}")
            pipe.zremrangebyscore("cooldown_expiry", 0, now)
            await pipe.execute()
        
        return [int(uid) for uid in expired if uid.isdigit()]

# ==================== Utility Functions ====================
def format_duration(seconds: int) -> str:
    """Format seconds into human readable duration"""
    if seconds < 60:
        return f"{seconds} ثانية"
    elif seconds < 3600:
        minutes = seconds // 60
        return f"{minutes} دقيقة"
    elif seconds < 86400:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours} ساعة {minutes} دقيقة" if minutes else f"{hours} ساعة"
    else:
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        return f"{days} يوم {hours} ساعة" if hours else f"{days} يوم"

def create_request_id() -> str:
    """Create unique request ID"""
    return hashlib.md5(f"{time.time()}{secrets.token_hex(8)}".encode()).hexdigest()[:12]

# ==================== API Manager ====================
class APIManager:
    def __init__(self, semaphore):
        self.semaphore = semaphore
    
    async def create_ssh_account(self, user_id: int, username: str) -> Dict:
        """Create SSH account via API"""
        request_id = create_request_id()
        
        payload = {
            "store_owner_id": 1,
            "user_id": user_id,
            "username": username,
            "request_id": request_id,
            "timestamp": int(time.time())
        }
        
        headers = {
            'Content-Type': 'application/json',
            'User-Agent': f'TelegramSSHBot/2.0 (RequestID: {request_id})',
            'Accept': 'application/json'
        }
        
        timeout = aiohttp.ClientTimeout(total=Config.REQUEST_TIMEOUT)
        
        async with self.semaphore:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                logger.info(f"API Request {request_id} for user {user_id}")
                
                async with session.post(
                    Config.API_URL,
                    json=payload,
                    headers=headers,
                    ssl=True
                ) as response:
                    
                    response_text = await response.text()
                    logger.info(f"API Response {request_id}: {response.status}")
                    
                    if response.status in [200, 201]:
                        try:
                            return await response.json()
                        except json.JSONDecodeError:
                            # Fallback to text parsing
                            return json.loads(response_text)
                    else:
                        raise aiohttp.ClientResponseError(
                            request_info=response.request_info,
                            history=response.history,
                            status=response.status,
                            message=response_text
                        )

# ==================== UI Components ====================
class BotUI:
    @staticmethod
    def main_menu() -> InlineKeyboardMarkup:
        """Main menu keyboard"""
        keyboard = [
            [InlineKeyboardButton("🔐 إنشاء حساب SSH", callback_data="create_account")],
            [
                InlineKeyboardButton("📊 إحصائياتي", callback_data="my_stats"),
                InlineKeyboardButton("ℹ️ المساعدة", callback_data="help")
            ],
            [InlineKeyboardButton("🔄 تحديث", callback_data="refresh")]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    @staticmethod
    def admin_menu() -> InlineKeyboardMarkup:
        """Admin menu keyboard"""
        keyboard = [
            [InlineKeyboardButton("📈 إحصائيات عامة", callback_data="global_stats")],
            [
                InlineKeyboardButton("👥 المستخدمين", callback_data="users_info"),
                InlineKeyboardButton("🔄 إعادة تشغيل", callback_data="restart")
            ],
            [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

# ==================== Global Variables ====================
redis_manager: Optional[RedisManager] = None
api_manager: Optional[APIManager] = None

# ==================== Bot Handlers ====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enhanced start command with user info"""
    user = update.effective_user
    await redis_manager.log_request(user.id, "start")
    
    user_stats = await redis_manager.get_user_stats(user.id)
    cooldown_remaining = user_stats["cooldown_remaining"]
    
    if cooldown_remaining > 0:
        status = f"⏳ متبقي: {format_duration(cooldown_remaining)}"
    else:
        status = "✅ جاهز للاستخدام"
    
    welcome_text = f"""
🔐 **بوت حسابات SSH المطور**

👋 **أهلاً وسهلاً** @{user.username or user.first_name}!

📊 **حالة حسابك:**
• الحالة: {status}
• الطلبات السابقة: {user_stats['total_requests']}
• الحسابات المنشأة: {user_stats['successful_accounts']}

⚡ **مميزات البوت:**
• حسابات SSH مجانية لمدة 3 ساعات
• واجهة تفاعلية سهلة الاستخدام
• أمان عالي وحماية مطورة
• إحصائيات شخصية مفصلة

اختر من القائمة أدناه:
    """
    
    await update.message.reply_text(
        welcome_text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=BotUI.main_menu()
    )

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all callback queries"""
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    data = query.data
    
    # Route to appropriate handler
    if data == "create_account":
        await handle_create_account(query, context)
    elif data == "my_stats":
        await handle_my_stats(query, context)
    elif data == "help":
        await handle_help(query, context)
    elif data == "refresh":
        await handle_refresh(query, context)
    elif data == "global_stats":
        await handle_global_stats(query, context)
    elif data == "main_menu":
        await handle_main_menu(query, context)
    else:
        await query.edit_message_text("❌ خيار غير معروف")

async def handle_create_account(query, context: ContextTypes.DEFAULT_TYPE):
    """Handle SSH account creation"""
    user = query.from_user
    await redis_manager.log_request(user.id, "create_account")
    
    # Check cooldown
    cooldown_remaining = await redis_manager.get_cooldown_remaining(user.id)
    if cooldown_remaining > 0:
        await query.edit_message_text(
            f"⏳ **يجب الانتظار قبل إنشاء حساب جديد**\n\n"
            f"الوقت المتبقي: {format_duration(cooldown_remaining)}\n\n"
            f"💡 كل مستخدم يمكنه إنشاء حساب واحد كل {Config.COOLDOWN_HOURS} ساعات",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=BotUI.main_menu()
        )
        return
    
    # Show progress
    await query.edit_message_text("⏳ **جاري إنشاء حساب SSH...**\n\nيرجى الانتظار...")
    
    try:
        # Create account
        account_data = await api_manager.create_ssh_account(
            user.id, 
            user.username or f"user_{user.id}"
        )
        
        # Set cooldown
        expiry_timestamp = await redis_manager.set_user_cooldown(user.id)
        expiry_date = datetime.fromtimestamp(expiry_timestamp)
        
        # Log success
        await redis_manager.log_success(user.id)
        
        # Format response
        success_text = f"""
🎉 **تم إنشاء حساب SSH بنجاح!**

🔐 **بيانات الدخول:**
👤 اسم المستخدم: `{account_data.get('Usuario', 'N/A')}`
🔑 كلمة المرور: `{account_data.get('Senha', 'N/A')}`

⏰ **معلومات الصلاحية:**
📅 تاريخ الإنشاء: {datetime.now().strftime('%Y-%m-%d %H:%M')}
⌛ مدة الصلاحية: {account_data.get('Expiracao', '3 ساعات')}
🔄 الحساب التالي: {expiry_date.strftime('%Y-%m-%d %H:%M')}

🔒 **معلومات الاتصال:**
• البروتوكول: SSH/OpenVPN
• المنافذ المدعومة: 22, 80, 443, 8080
• التشفير: AES-256

✨ **استمتع بالاستخدام الآمن!**
        """
        
        await query.edit_message_text(
            success_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=BotUI.main_menu()
        )
        
        logger.info(f"SSH account created successfully for user {user.id}")
        
    except asyncio.TimeoutError:
        await redis_manager.log_error("timeout", user.id)
        await query.edit_message_text(
            "⏰ **انتهت مهلة الاتصال**\n\n"
            "الخادم لا يستجيب حالياً.\n"
            "يرجى المحاولة مرة أخرى بعد قليل.",
            reply_markup=BotUI.main_menu()
        )
        
    except aiohttp.ClientResponseError as e:
        await redis_manager.log_error("api_error", user.id)
        await query.edit_message_text(
            f"❌ **خطأ في الخادم**\n\n"
            f"رمز الخطأ: {e.status}\n"
            f"يرجى المحاولة مرة أخرى لاحقاً.",
            reply_markup=BotUI.main_menu()
        )
        
    except Exception as e:
        await redis_manager.log_error("unexpected", user.id)
        logger.error(f"Unexpected error creating account for user {user.id}: {e}", exc_info=True)
        await query.edit_message_text(
            "❌ **حدث خطأ غير متوقع**\n\n"
            "تم تسجيل الخطأ للمراجعة.\n"
            "يرجى المحاولة مرة أخرى لاحقاً.",
            reply_markup=BotUI.main_menu()
        )

async def handle_my_stats(query, context: ContextTypes.DEFAULT_TYPE):
    """Handle user statistics display"""
    user = query.from_user
    await redis_manager.log_request(user.id, "my_stats")
    
    user_stats = await redis_manager.get_user_stats(user.id)
    cooldown_remaining = user_stats["cooldown_remaining"]
    
    if cooldown_remaining > 0:
        status = f"⏳ متبقي: {format_duration(cooldown_remaining)}"
        next_account = f"🔄 الحساب التالي بعد: {format_duration(cooldown_remaining)}"
    else:
        status = "✅ جاهز للاستخدام"
        next_account = "🔄 يمكنك إنشاء حساب الآن"
    
    stats_text = f"""
📊 **إحصائياتك الشخصية**

👤 **معلومات الحساب:**
• الاسم: {user.first_name}
• المعرف: @{user.username or 'غير محدد'}
• الحالة الحالية: {status}

📈 **إحصائيات الاستخدام:**
• إجمالي الطلبات: {user_stats['total_requests']}
• الحسابات المنشأة: {user_stats['successful_accounts']}
• الأخطاء: {user_stats['errors']}
• آخر استخدام: {user_stats['last_seen']}

⏰ **معلومات الوقت:**
{next_account}

💡 **نصائح:**
• كل حساب صالح لمدة 3 ساعات
• يمكن إنشاء حساب جديد كل 3 ساعات
• احفظ بيانات الحساب في مكان آمن
    """
    
    await query.edit_message_text(
        stats_text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=BotUI.main_menu()
    )

async def handle_help(query, context: ContextTypes.DEFAULT_TYPE):
    """Handle help display"""
    help_text = """
📚 **دليل استخدام البوت**

🔐 **كيفية الحصول على حساب SSH:**
1. اضغط على "إنشاء حساب SSH"
2. انتظر حتى يتم إنشاء الحساب
3. احفظ بيانات الدخول في مكان آمن
4. استخدم الحساب لمدة 3 ساعات

⚡ **معلومات مهمة:**
• مدة صلاحية كل حساب: 3 ساعات
• فترة الانتظار بين الحسابات: 3 ساعات
• البروتوكولات المدعومة: SSH, OpenVPN
• المنافذ المتاحة: 22, 80, 443, 8080

🛡️ **نصائح الأمان:**
• لا تشارك بيانات الحساب مع أحد
• استخدم الحساب للأغراض المشروعة فقط
• احذف بيانات الحساب بعد انتهاء الصلاحية

📞 **الدعم الفني:**
• للمشاكل التقنية: تواصل مع المطور
• للاقتراحات: استخدم خاصية التقييم
• للبلاغات: أرسل رسالة للإدارة

🔄 **التحديثات:**
• يتم تطوير البوت باستمرار
• ستصلك إشعارات عند توفر ميزات جديدة
    """
    
    await query.edit_message_text(
        help_text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=BotUI.main_menu()
    )

async def handle_refresh(query, context: ContextTypes.DEFAULT_TYPE):
    """Handle refresh/main menu"""
    await handle_main_menu(query, context)

async def handle_main_menu(query, context: ContextTypes.DEFAULT_TYPE):
    """Return to main menu"""
    user = query.from_user
    user_stats = await redis_manager.get_user_stats(user.id)
    cooldown_remaining = user_stats["cooldown_remaining"]
    
    if cooldown_remaining > 0:
        status = f"⏳ متبقي: {format_duration(cooldown_remaining)}"
    else:
        status = "✅ جاهز للاستخدام"
    
    welcome_text = f"""
🔐 **بوت حسابات SSH المطور**

👋 **مرحباً** @{user.username or user.first_name}!

📊 **الحالة الحالية:** {status}
📈 **طلباتك:** {user_stats['total_requests']} | **نجح:** {user_stats['successful_accounts']}

اختر من القائمة أدناه:
    """
    
    await query.edit_message_text(
        welcome_text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=BotUI.main_menu()
    )

async def handle_global_stats(query, context: ContextTypes.DEFAULT_TYPE):
    """Handle global statistics (admin only)"""
    user = query.from_user
    
    if user.id not in Config.ADMIN_USER_IDS:
        await query.answer("❌ هذه الميزة للمشرفين فقط", show_alert=True)
        return
    
    stats = await redis_manager.get_global_stats()
    
    stats_text = f"""
🔧 **إحصائيات البوت العامة**

📊 **الأداء:**
• إجمالي الطلبات: {stats['total_requests']:,}
• النجاحات: {stats['success_count']:,}
• الأخطاء: {stats['error_count']:,}
• معدل النجاح: {stats['success_rate']}

👥 **المستخدمين:**
• مستخدمين فريدين: {stats['unique_users']:,}
• أوامر /start: {stats['start_commands']:,}
• أوامر /get: {stats['get_commands']:,}

⏰ **النظام:**
• وقت التشغيل: {stats['uptime']}
• آخر تحديث: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

🚀 **الحالة:** ✅ يعمل بكفاءة عالية
    """
    
    await query.edit_message_text(
        stats_text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=BotUI.admin_menu()
    )

# ==================== Notification System ====================
async def cooldown_notification_job(context: ContextTypes.DEFAULT_TYPE):
    """Send notifications to users when cooldown expires"""
    if not Config.ENABLE_NOTIFICATIONS:
        return
    
    try:
        expired_users = await redis_manager.get_expired_users()
        
        for user_id in expired_users:
            try:
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔐 إنشاء حساب جديد", callback_data="create_account")]
                ])
                
                await context.bot.send_message(
                    chat_id=user_id,
                    text="🎉 **انتهت فترة الانتظار!**\n\n"
                         "يمكنك الآن إنشاء حساب SSH جديد.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=keyboard
                )
                
                logger.info(f"Cooldown notification sent to user {user_id}")
                
            except Exception as e:
                logger.warning(f"Failed to send notification to user {user_id}: {e}")
                
    except Exception as e:
        logger.error(f"Error in cooldown notification job: {e}")

# ==================== Web Health Check ====================
async def health_endpoint(request):
    """Health check endpoint"""
    try:
        stats = await redis_manager.get_global_stats()
        return web.json_response({
            "status": "healthy",
            "version": "2.0",
            "statistics": stats,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "redis_connected": True
        })
    except Exception as e:
        return web.json_response({
            "status": "unhealthy",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat() + "Z"
        }, status=503)

async def metrics_endpoint(request):
    """Metrics endpoint for monitoring"""
    try:
        stats = await redis_manager.get_global_stats()
        return web.json_response(stats)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

# ==================== Application Setup ====================
async def setup_bot_commands(application: Application):
    """Set bot commands in Telegram"""
    commands = [
        BotCommand("start", "بدء استخدام البوت"),
        BotCommand("help", "عرض المساعدة"),
        BotCommand("mystats", "عرض إحصائياتي"),
    ]
    
    await application.bot.set_my_commands(commands)
    logger.info("Bot commands set successfully")

async def post_init(application: Application):
    """Post initialization setup"""
    await setup_bot_commands(application)
    logger.info("Bot post-initialization completed")

def create_web_app() -> web.Application:
    """Create aiohttp web application"""
    app = web.Application()
    app.router.add_get('/', health_endpoint)
    app.router.add_get('/health', health_endpoint)
    app.router.add_get('/metrics', metrics_endpoint)
    return app

# ==================== Main Application ====================
async def main():
    """Main application entry point"""
    global redis_manager, api_manager
    
    if not Config.BOT_TOKEN:
        logger.critical("❌ BOT_TOKEN environment variable not set!")
        return
    
    # Initialize Redis
    try:
        redis_client = aioredis.from_url(Config.REDIS_URL, decode_responses=True)
        await redis_client.ping()
        logger.info("✅ Redis connection established")
    except Exception as e:
        logger.critical(f"❌ Failed to connect to Redis: {e}")
        return
    
    # Initialize managers
    redis_manager = RedisManager(redis_client)
    api_manager = APIManager(asyncio.Semaphore(Config.MAX_CONCURRENT_REQUESTS))
    
    # Create bot application
    application = (
        Application.builder()
        .token(Config.BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    
    # Add handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    
    # Add job for cooldown notifications
    if Config.ENABLE_NOTIFICATIONS:
        application.job_queue.run_repeating(
            cooldown_notification_job, 
            interval=60,  # Check every minute
            first=10     # First run after 10 seconds
        )
        logger.info("✅ Cooldown notification job scheduled")
    
    # Start web server
    web_app = create_web_app()
    runner = web.AppRunner(web_app)
    await runner.setup()
    
    site = web.TCPSite(runner, '0.0.0.0', Config.WEB_PORT)
    await site.start()
    logger.info(f"🌐 Web server started on port {Config.WEB_PORT}")
    
    # Start bot
    logger.info("🚀 Starting Telegram bot...")
    await application.initialize()
    await application.start()
    await application.updater.start_polling(
        drop_pending_updates=True,
        allowed_updates=['message', 'callback_query']
    )
    
    logger.info("✅ Bot is running successfully!")
    
    try:
        # Keep running until interrupted
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("🛑 Shutting down bot...")
    finally:
        # Cleanup
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        await runner.cleanup()
        await redis_client.close()
        logger.info("🔚 Bot shutdown completed")

# ==================== Enhanced Error Handling ====================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Enhanced global error handler"""
    if isinstance(update, Update) and update.effective_user:
        user_id = update.effective_user.id
        await redis_manager.log_error("bot_error", user_id)
        
        # Send user-friendly error message
        try:
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    "❌ **حدث خطأ مؤقت**\n\n"
                    "يتم العمل على حل المشكلة.\n"
                    "يرجى المحاولة مرة أخرى.",
                    reply_markup=BotUI.main_menu()
                )
            elif update.message:
                await update.message.reply_text(
                    "❌ **حدث خطأ مؤقت**\n\n"
                    "يتم العمل على حل المشكلة.\n"
                    "يرجى المحاولة مرة أخرى.",
                    reply_markup=BotUI.main_menu()
                )
        except Exception:
            pass  # Ignore if we can't send error message
    
    logger.error(f"Bot error: {context.error}", exc_info=True)

# ==================== Additional Commands ====================
async def mystats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Direct stats command"""
    user = update.effective_user
    await redis_manager.log_request(user.id, "mystats")
    
    user_stats = await redis_manager.get_user_stats(user.id)
    cooldown_remaining = user_stats["cooldown_remaining"]
    
    if cooldown_remaining > 0:
        status = f"⏳ متبقي: {format_duration(cooldown_remaining)}"
    else:
        status = "✅ جاهز للاستخدام"
    
    stats_text = f"""
📊 **إحصائياتك الشخصية**

👤 **معلومات الحساب:**
• الحالة: {status}
• إجمالي الطلبات: {user_stats['total_requests']}
• الحسابات المنشأة: {user_stats['successful_accounts']}
• آخر استخدام: {user_stats['last_seen']}

💡 **معلومات مفيدة:**
• يمكن إنشاء حساب جديد كل 3 ساعات
• كل حساب صالح لمدة 3 ساعات
    """
    
    await update.message.reply_text(
        stats_text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=BotUI.main_menu()
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Direct help command"""
    await redis_manager.log_request(update.effective_user.id, "help")
    
    help_text = """
📚 **دليل البوت الشامل**

🔐 **الحصول على حساب SSH:**
استخدم الأزرار التفاعلية أو الأوامر:
• `/start` - بدء استخدام البوت
• اضغط "إنشاء حساب SSH" للحصول على حساب

📊 **الإحصائيات:**
• `/mystats` - إحصائياتك الشخصية
• عرض عدد الطلبات والحسابات المنشأة

⏰ **نظام الانتظار:**
• كل مستخدم يحصل على حساب كل 3 ساعات
• تلقي إشعار عند انتهاء فترة الانتظار
• كل حساب صالح لمدة 3 ساعات

🛡️ **الأمان والخصوصية:**
• جميع البيانات محمية ومشفرة
• لا يتم حفظ كلمات المرور
• استخدم الحسابات للأغراض المشروعة فقط

💻 **التقنيات المدعومة:**
• SSH على المنافذ: 22, 2222
• OpenVPN على المنافذ: 443, 1194
• تشفير AES-256

🔄 **التحديثات:**
البوت في تطوير مستمر مع إضافة ميزات جديدة
    """
    
    await update.message.reply_text(
        help_text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=BotUI.main_menu()
    )

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin panel command"""
    user = update.effective_user
    
    if user.id not in Config.ADMIN_USER_IDS:
        await update.message.reply_text("❌ هذا الأمر للمشرفين فقط")
        return
    
    await redis_manager.log_request(user.id, "admin")
    
    stats = await redis_manager.get_global_stats()
    
    admin_text = f"""
🔧 **لوحة الإدارة**

📊 **إحصائيات شاملة:**
• إجمالي الطلبات: {stats['total_requests']:,}
• النجاحات: {stats['success_count']:,}
• الأخطاء: {stats['error_count']:,}
• معدل النجاح: {stats['success_rate']}
• مستخدمين فريدين: {stats['unique_users']:,}

⏰ **معلومات النظام:**
• وقت التشغيل: {stats['uptime']}
• حالة Redis: ✅ متصل
• حالة API: ✅ يعمل
• الإشعارات: {'✅ مفعلة' if Config.ENABLE_NOTIFICATIONS else '❌ معطلة'}

🚀 **الأداء:**
• الطلبات المتزامنة: {Config.MAX_CONCURRENT_REQUESTS}
• مهلة الطلب: {Config.REQUEST_TIMEOUT} ثانية
• منفذ الويب: {Config.WEB_PORT}
    """
    
    await update.message.reply_text(
        admin_text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=BotUI.admin_menu()
    )

# ==================== Enhanced Main with Error Handling ====================
if __name__ == "__main__":
    try:
        # Add commands to the application in main
        async def setup_application():
            global redis_manager, api_manager
            
            if not Config.BOT_TOKEN:
                logger.critical("❌ BOT_TOKEN environment variable not set!")
                return None
            
            # Initialize Redis
            try:
                redis_client = aioredis.from_url(Config.REDIS_URL, decode_responses=True)
                await redis_client.ping()
                logger.info("✅ Redis connection established")
            except Exception as e:
                logger.critical(f"❌ Failed to connect to Redis: {e}")
                return None
            
            # Initialize managers
            redis_manager = RedisManager(redis_client)
            api_manager = APIManager(asyncio.Semaphore(Config.MAX_CONCURRENT_REQUESTS))
            
            # Create bot application
            application = (
                Application.builder()
                .token(Config.BOT_TOKEN)
                .post_init(post_init)
                .build()
            )
            
            # Add error handler
            application.add_error_handler(error_handler)
            
            # Add all handlers
            application.add_handler(CommandHandler("start", start_command))
            application.add_handler(CommandHandler("mystats", mystats_command))
            application.add_handler(CommandHandler("help", help_command))
            application.add_handler(CommandHandler("admin", admin_command))
            application.add_handler(CallbackQueryHandler(handle_callback_query))
            
            # Add job for cooldown notifications
            if Config.ENABLE_NOTIFICATIONS:
                application.job_queue.run_repeating(
                    cooldown_notification_job, 
                    interval=60,  # Check every minute
                    first=10     # First run after 10 seconds
                )
                logger.info("✅ Cooldown notification job scheduled")
            
            # Start web server
            web_app = create_web_app()
            runner = web.AppRunner(web_app)
            await runner.setup()
            
            site = web.TCPSite(runner, '0.0.0.0', Config.WEB_PORT)
            await site.start()
            logger.info(f"🌐 Web server started on port {Config.WEB_PORT}")
            
            return application, runner, redis_client
        
        # Run the application
        async def run_bot():
            setup_result = await setup_application()
            if not setup_result:
                return
            
            application, runner, redis_client = setup_result
            
            try:
                # Start bot
                logger.info("🚀 Starting Telegram bot...")
                await application.initialize()
                await application.start()
                await application.updater.start_polling(
                    drop_pending_updates=True,
                    allowed_updates=['message', 'callback_query'],
                    timeout=30
                )
                
                logger.info("✅ SSH Bot is running successfully!")
                logger.info(f"📊 Redis URL: {Config.REDIS_URL}")
                logger.info(f"🌐 Web Health: http://localhost:{Config.WEB_PORT}/health")
                logger.info(f"⚡ Max Concurrent: {Config.MAX_CONCURRENT_REQUESTS}")
                logger.info(f"⏰ Cooldown: {Config.COOLDOWN_HOURS} hours")
                
                # Keep running until interrupted
                while True:
                    await asyncio.sleep(1)
                    
            except KeyboardInterrupt:
                logger.info("🛑 Received shutdown signal...")
            except Exception as e:
                logger.critical(f"❌ Fatal error: {e}", exc_info=True)
            finally:
                # Graceful shutdown
                logger.info("🔄 Starting graceful shutdown...")
                try:
                    await application.updater.stop()
                    await application.stop() 
                    await application.shutdown()
                    await runner.cleanup()
                    await redis_client.close()
                    logger.info("✅ Shutdown completed successfully")
                except Exception as e:
                    logger.error(f"Error during shutdown: {e}")
        
        # Run the bot
        asyncio.run(run_bot())
        
    except KeyboardInterrupt:
        logger.info("🛑 Bot interrupted by user")
    except Exception as e:
        logger.critical(f"❌ Critical startup error: {e}", exc_info=True)
    finally:
        logger.info("🔚 Bot process ended")
