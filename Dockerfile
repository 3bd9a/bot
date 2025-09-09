FROM python:3.11-slim

WORKDIR /app

# تثبيت أدوات أساسية للبناء (عشان aioredis وبعض المكتبات)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
