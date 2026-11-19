FROM python:3.12-slim

# Системные зависимости (для psycopg2 на slim и для работы с видео)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала зависимости — для кэша слоёв
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Потом всё остальное (x.py, red-1.mp4)
COPY . .

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Render передаёт PORT через env, но экспозим на всякий
EXPOSE 10000

CMD ["python", "x.py"]
