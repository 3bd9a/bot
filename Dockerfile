FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY bot.py ./

ENV BOT_TOKEN=your_bot_token_here
ENV API_URL=https://painel.meowssh.shop:5000/test_ssh_public
ENV REDIS_URL=redis://localhost:6379

CMD ["python", "bot.py"]
