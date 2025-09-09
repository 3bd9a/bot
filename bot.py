from flask import Flask
from threading import Thread
from telegram.ext import Application
import os

# خادم ويب لل health checks
app = Flask(__name__)

@app.route('/')
def health_check():
    return '✅ Bot is alive', 200

def run_web():
    app.run(host='0.0.0.0', port=8000)

# ابدأ خادم الويب في thread منفصل
Thread(target=run_web, daemon=True).start()

# كود البوت العادي
TOKEN = os.getenv('BOT_TOKEN')

def main():
    app = Application.builder().token(TOKEN).build()
    print("🟢 البوت يعمل...")
    app.run_polling()

if __name__ == "__main__":
    main()
