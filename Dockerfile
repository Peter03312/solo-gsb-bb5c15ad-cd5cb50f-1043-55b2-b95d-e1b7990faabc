FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/changeover.db \
    HOST=0.0.0.0 \
    PORT=8080

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# SQLite 数据持久化目录（compose 在此挂载卷；首次启动自动建表初始化）
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

# 容器内默认写入演示原料批号与产线，便于一键启动后直接体验
CMD ["sh", "-c", "SEED_DEMO_DATA=${SEED_DEMO_DATA:-1} python -m app"]
