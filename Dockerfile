# استخدم Python 3.11 slim
FROM python:3.11-slim

# تعيين مجلد العمل
WORKDIR /app

# نسخ الملفات
COPY bot.py ./
COPY requirements.txt ./

# تثبيت المتطلبات
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# تعيين ENV variables افتراضية (يمكن تعديلها من Koyeb)
ENV BOT_TOKEN=""
ENV API_URL=""
ENV REDIS_URL="redis://localhost:6379"

# تعريض منفذ health check
EXPOSE 8000

# الأمر لتشغيل البوت
CMD ["python", "bot.py"]
