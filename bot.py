from flask import Flask
from threading import Thread
from telegram.ext import Application, CommandHandler
from telegram import Update
from telegram.ext import ContextTypes
import os
import logging

# تفعيل logging لرؤية الأخطاء
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# خادم ويب لل health checks
app = Flask(__name__)

@app.route('/')
def health_check():
    return '✅ Bot is alive', 200

def run_web():
    app.run(host='0.0.0.0', port=8000)

# ابدأ خادم الويب في thread منفصل
Thread(target=run_web, daemon=True).start()

# كود البوت
TOKEN = os.getenv('BOT_TOKEN')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🎉 البوت يعمل بنجاح! أخبرني كيف يمكنني مساعدتك؟')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
    📋 الأوامر المتاحة:
    /start - بدء البوت
    /help - عرض هذه المساعدة
    /info - معلومات عن البوت
    """
    await update.message.reply_text(help_text)

def main():
    try:
        # إنشاء البوت
        application = Application.builder().token(TOKEN).build()
        
        # إضافة handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        
        logging.info("🟢 البوت يبدأ...")
        application.run_polling()
        
    except Exception as e:
        logging.error(f"❌ خطأ: {e}")

if __name__ == "__main__":
    main()
