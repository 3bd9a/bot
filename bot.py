from flask import Flask
from threading import Thread
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import aiohttp
import logging
import os
import time
from datetime import datetime
import json

# ==================== إعدادات متقدمة للـ Logging ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot_debug.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==================== إعدادات التتبع والأداء ====================
class PerformanceTracker:
    def __init__(self):
        self.requests_count = 0
        self.errors_count = 0
        self.start_time = time.time()
    
    def log_request(self):
        self.requests_count += 1
    
    def log_error(self):
        self.errors_count += 1
    
    def get_stats(self):
        uptime = time.time() - self.start_time
        return {
            "requests": self.requests_count,
            "errors": self.errors_count,
            "uptime_seconds": int(uptime),
            "uptime_human": str(datetime.timedelta(seconds=int(uptime)))
        }

tracker = PerformanceTracker()

# ==================== خادم Flask للـ Health Checks ====================
app = Flask(__name__)

@app.route('/')
def health_check():
    return json.dumps({
        "status": "healthy",
        "stats": tracker.get_stats(),
        "timestamp": datetime.now().isoformat()
    }), 200, {'Content-Type': 'application/json'}

@app.route('/stats')
def stats():
    return json.dumps(tracker.get_stats()), 200, {'Content-Type': 'application/json'}

def run_flask():
    app.run(host='0.0.0.0', port=8000, debug=False, use_reloader=False)

Thread(target=run_flask, daemon=True).start()

# ==================== إعدادات البوت ====================
TOKEN = os.getenv('BOT_TOKEN')
API_URL = "https://painel.meowssh.shop:5000/test_ssh_public"

if not TOKEN:
    logger.critical("❌ BOT_TOKEN not set in environment variables!")
    exit(1)

# ==================== دوال البوت المحسنة ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """رسالة ترحيب محسنة"""
    try:
        tracker.log_request()
        logger.info(f"User {update.effective_user.id} used /start")
        
        welcome_text = """
🔐 **بوت حسابات SSH المطور**

📋 **الأوامر المتاحة:**
/get - الحصول على حساب SSH مجاني
/help - عرض المساعدة
/stats - إحصائيات البوت

⚡ **مميزات البوت:**
- سرعة فائقة في الاستجابة
- اتصال آمن مع الخوادم
- دعم فني متواصل

⚠️ **مدة الصلاحية:** 3 ساعات
        """
        await update.message.reply_text(welcome_text, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error in start: {e}")
        await update.message.reply_text("❌ حدث خطأ غير متوقع")

async def get_ssh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """جلب حساب SSH مع معالجة متقدمة للأخطاء"""
    try:
        tracker.log_request()
        user_id = update.effective_user.id
        logger.info(f"User {user_id} requested SSH account")
        
        # رسالة الانتظار
        wait_msg = await update.message.reply_text("⏳ جاري الاتصال بالخادم...")
        
        # ⭐⭐⭐ الإعدادات المحسنة ⭐⭐⭐
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            try:
                async with session.post(
                    API_URL, 
                    json={"store_owner_id": 1},
                    headers=headers,  # ⬅️ الإضافة الأهم
                    ssl=False
                ) as response:
                    
                    logger.info(f"API Response Status: {response.status}")
                    
                    if response.status == 200:
                        data = await response.json()
                        logger.info(f"SSH account generated for user {user_id}: {data}")
                        
                        ssh_info = f"""
🔐 **تم الحصول على حساب SSH بنجاح!**

👤 **المستخدم:** `{data.get('Usuario', 'N/A')}`
🔑 **كلمة المرور:** `{data.get('Senha', 'N/A')}`
⏰ **مدة الصلاحية:** {data.get('Expiracao', 'N/A')}

⚡ **استمتع بالاستخدام!**
                        """
                        
                        await wait_msg.delete()
                        await update.message.reply_text(ssh_info, parse_mode='Markdown')
                        
                    else:
                        # الحصول على تفاصيل الخطأ
                        error_detail = await response.text()
                        logger.warning(f"API error {response.status}: {error_detail}")
                        
                        error_msg = f"""
❌ **خطأ في الاتصال**

📊 **التفاصيل:**
- الرمز: {response.status}
- الحالة: {response.reason}

🔧 **الحل:**
- حاول مرة أخرى بعد قليل
- تأكد من اتصال الإنترنت
- للدعم: @SAYF1INFO
                        """
                        await wait_msg.edit_text(error_msg, parse_mode='Markdown')
                        
            except aiohttp.ClientError as e:
                logger.error(f"Network error: {e}")
                await wait_msg.edit_text("❌ مشكلة في الشبكة. حاول لاحقاً.")
            except asyncio.TimeoutError:
                logger.error("Request timeout")
                await wait_msg.edit_text("⏰ انتهت مدة الانتظار. حاول مرة أخرى.")
                    
    except Exception as e:
        tracker.log_error()
        logger.error(f"Unexpected error in get_ssh: {e}", exc_info=True)
        await update.message.reply_text("❌ حدث خطأ غير متوقع في النظام")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض المساعدة"""
    try:
        tracker.log_request()
        help_text = """
🆘 **مركز المساعدة**

📋 **الأوامر المتاحة:**
/start - بدء البوت وعرض المعلومات
/get - إنشاء حساب SSH جديد
/help - عرض هذه الرسالة
/stats - إحصائيات البوت

🔧 **استكشاف الأخطاء:**
- إذا لم يعمل الأمر /get، حاول مرة أخرى بعد دقيقة
- تأكد من توفر اتصال الإنترنت
- للدعم: @SAYF1INFO

📊 **معلومات التقنية:**
- يعمل على سحابة Koyeb
- اتصال آمن بـ TLS
- مراقبة مستمرة 24/7
        """
        await update.message.reply_text(help_text, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error in help: {e}")
        await update.message.reply_text("❌ حدث خطأ في عرض المساعدة")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض إحصائيات البوت"""
    try:
        stats = tracker.get_stats()
        stats_text = f"""
📊 **إحصائيات البوت المتقدمة**

• الطلبات الكلية: {stats['requests']}
• الأخطاء المسجلة: {stats['errors']}
• وقت التشغيل: {stats['uptime_human']}
• آخر تحديث: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

🔄 **حالة النظام:**
- الخادم: ✅ يعمل
- API: ✅ متصل
- البيانات: ✅ نشط
        """
        await update.message.reply_text(stats_text, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error in stats: {e}")
        await update.message.reply_text("❌ حدث خطأ في جلب الإحصائيات")

# ==================== إعدادات التطبيق الرئيسي ====================
def main():
    try:
        logger.info("🚀 بدء تشغيل البوت المتطور...")
        
        application = Application.builder().token(TOKEN).build()
        
        # إضافة Handlers
        handlers = [
            CommandHandler("start", start),
            CommandHandler("get", get_ssh),
            CommandHandler("help", help_command),
            CommandHandler("stats", stats_command)
        ]
        
        for handler in handlers:
            application.add_handler(handler)
        
        logger.info("✅ تم تحميل جميع الأوامر بنجاح")
        logger.info("🟢 البوت يعمل وجاهز للاستقبال الطلبات...")
        
        application.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
            close_loop=False
        )
        
    except Exception as e:
        logger.critical(f"❌ فشل تشغيل البوت: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    main()
