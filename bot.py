from telegram.ext import Application, CommandHandler
import os
import asyncio

TOKEN = os.getenv('BOT_TOKEN')

async def start(update, context):
    await update.message.reply_text('مرحباً! البوت يعمل بدون Docker ✅')

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    
    print("البوت يعمل...")
    app.run_polling()

if __name__ == "__main__":
    main()
