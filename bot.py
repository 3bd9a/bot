from flask import Flask
from threading import Thread
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import aiohttp
import logging
import os  # ⬅️ Important: add this import!

# تفعيل logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# خادم Flask لل health checks
app = Flask(__name__)

@app.route('/')
def health_check():
    return '✅ Bot is alive', 200

def run_flask():
    app.run(host='0.0.0.0', port=8000)

# تشغيل Flask في thread منفصل
Thread(target=run_flask, daemon=True).start()

# ⬇️ التصحيح هنا: أخذ التوكن من environment variables
TOKEN = os.getenv('BOT_TOKEN')  # ✅ صحيح
API_URL = "https://painel.meowssh.shop:5000/test_ssh_public"

# ⬇️ إضافة تحقق من التوكن
if not TOKEN:
    logging.error("❌ BOT_TOKEN not set in environment variables!")
    exit(1)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """رسالة الترحيب"""
    welcome_text = """
    🔐 **بوت حسابات SSH**
    
    📋 **الأوامر المتاحة:**
    /get - الحصول على حساب SSH مجاني
    /help - عرض المساعدة
    
    ⚠️ **مدة الصلاحية:** 3 ساعات
    """
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def get_ssh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """جلب حساب SSH"""
    try:
        # إرسال رسالة الانتظار
        wait_msg = await update.message.reply_text("⏳ جاري الاتصال بالخادم...")
        
        # جلب البيانات من API (المتجر 1 فقط)
        async with aiohttp.ClientSession() as session:
            async with session.post(API_URL, json={"store_owner_id": 1}) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    # تنسيق الرسالة (بدون IP)
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
                    await wait_msg.edit_text("❌ فشل الاتصال بالخادم. حاول لاحقاً.")
                    
    except Exception as e:
        logging.error(f"Error: {e}")
        await update.message.reply_text("❌ حدث خطأ. حاول مرة أخرى.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض المساعدة"""
    help_text = """
    🆘 **مساعدة البوت**
    
    📋 **الأوامر:**
    /start - بدء البوت
    /get - الحصول على حساب SSH مجاني
    /help - هذه الرسالة
    
    ⚠️ **معلومات:**
    - الحسابات مجانية بالكامل
    - مدة الصلاحية: 3 ساعات
    - يتم تجديد الحسابات تلقائياً
    """
    await update.message.reply_text(help_text, parse_mode='Markdown')

def main():
    """الدالة الرئيسية"""
    application = Application.builder().token(TOKEN).build()
    
    # إضافة ال handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("get", get_ssh))
    application.add_handler(CommandHandler("help", help_command))
    
    # بدء البوت
    logging.info("🟢 بوت SSH يعمل...")
    application.run_polling()

if __name__ == "__main__":
    main()
