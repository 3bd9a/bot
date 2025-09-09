from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import os

# جلب التوكن من متغير البيئة (آمن أكثر)
TOKEN = os.getenv('BOT_TOKEN')  # Important for Koyeb!

# أمر البدء
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('مرحباً! أنا بوتك الجديد على Koyeb. 😊')

# أمر المساعدة
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
    /start - بدء البوت
    /help - عرض الرسالة المساعدة
    /info - معلومات عن البوت
    """
    await update.message.reply_text(help_text)

# أمر جديد
async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('⚡ هذا البوت يعمل على Koyeb Cloud!')

# رد على الرسائل
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text.lower()
    
    if 'مرحبا' in user_message or 'اهلا' in user_message:
        await update.message.reply_text('أهلاً وسهلاً! كيف يمكنني مساعدتك؟')
    elif 'شكرا' in user_message:
        await update.message.reply_text('العفو! 😊')
    else:
        await update.message.reply_text('لم أفهم الرسالة، جرب /help للمساعدة.')

# معالجة الأخطاء
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"خطأ: {context.error}")

def main():
    # التأكد من وجود التوكن
    if not TOKEN:
        print("Error: BOT_TOKEN not set!")
        return
    
    # إنشاء التطبيق
    app = Application.builder().token(TOKEN).build()
    
    // إضافة الأوامر
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("info", info_command))
    app.add_handler(MessageHandler(filters.TEXT, handle_message))
    app.add_error_handler(error_handler)
    
    // البدء
    print("✅ البوت يعمل على Koyeb...")
    app.run_polling()

if __name__ == "__main__":
    main()