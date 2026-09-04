FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

ENV BOT_ENABLED=false DRY_RUN=true AUTO_POST=false AI_ENABLED=false
CMD ["python", "-m", "app.main", "--serve"]
