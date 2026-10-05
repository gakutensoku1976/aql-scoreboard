FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "fastapi==0.142.2" "starlette==1.7.0" "pydantic==2.13.5" "uvicorn[standard]==0.54.0"
COPY app.py game.py ./
COPY static ./static
ENV DB_PATH=/data/scoreboard.db
VOLUME /data
EXPOSE 8000
# 状態の書き込みを1プロセスに集約するため worker は1つ
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
