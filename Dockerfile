FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (better layer caching)
COPY eth-trader-bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot source into /app so gunicorn finds app:app directly
COPY eth-trader-bot/ .

RUN mkdir -p /data

ENV PORT=8080
EXPOSE 8080

CMD ["gunicorn", "app:app", "--config", "gunicorn.conf.py"]
