FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Webhook mode runs its own server on $PORT.
CMD ["python", "bot.py"]
