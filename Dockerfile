FROM python:3.11-slim

WORKDIR /app

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY frontend ./frontend

# data 目录挂出去做持久化
VOLUME ["/app/data", "/app/logs"]

EXPOSE 8000

CMD ["python", "-m", "backend.main"]
