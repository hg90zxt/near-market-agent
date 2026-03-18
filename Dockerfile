FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY autobot.py .

RUN mkdir -p /root/near-market-bot

ENV NEAR_KEY=""
ENV CLAUDE_KEY=""
ENV TG_BOT_TOKEN=""
ENV TG_CHAT_ID=""

CMD ["python3", "autobot.py"]
