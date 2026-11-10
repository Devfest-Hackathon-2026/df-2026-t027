# Cloud Run に載せる入れ物（開発手順書6-1）
FROM python:3.12-slim

WORKDIR /app

# 標準出力を貯めずにすぐ出す。Cloud Run のログは標準出力を読むため、
# 貯めると要求ごとのログ（backend/request_log.py）が遅れて出たり、落ちたときに消える
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリ（API と画面）だけを載せる。資料・SQL・スクリプト・テストは載せない（.dockerignore）
COPY 02_app ./02_app

# PORT は Cloud Run が渡す。exec を付けて uvicorn を PID 1 にする（停止の合図が届くようにする）
CMD exec uvicorn backend.main:app --app-dir 02_app --host 0.0.0.0 --port ${PORT:-8080}
