FROM python:3.12-slim

LABEL org.opencontainers.image.title="hy3api" \
      org.opencontainers.image.description="WorkBuddy / CodeBuddy CN / CodeBuddy 三平台 hy3 模型反代与号池面板"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    HY3API_DATA_DIR=/data \
    HY3API_HOST=0.0.0.0 \
    HY3API_PORT=8787

WORKDIR /app

# 先装依赖，利用镜像层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 后端源码
COPY app.py db.py providers.py proxy.py responses.py discovery.py trae.py ./

# 前端构建产物（app.py 的 WEB_DIR = <BASE_DIR>/frontend/dist）
# ⚠️ 构建镜像前必须先在本地执行：cd frontend && npm run build
#    若不想本地构建，改用 Dockerfile.fullbuild（容器内 node 阶段自动构建）
COPY frontend/dist/ ./frontend/dist/

# 构建期自检：dist 缺失/为空则立刻失败，避免部署出白屏面板
RUN test -s /app/frontend/dist/index.html \
    && ls /app/frontend/dist/assets/*.js >/dev/null \
    && ls /app/frontend/dist/assets/*.css >/dev/null \
    || (echo "ERROR: frontend/dist 不完整，请先执行 cd frontend && npm run build" && exit 1)

# 以非 root 运行；/data 为 SQLite 持久化目录
RUN useradd -r -u 10001 -m -d /home/hy3api hy3api \
    && mkdir -p /data \
    && chown -R hy3api:hy3api /app /data
USER hy3api

VOLUME ["/data"]
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/health',timeout=4).status==200 else 1)"

ENTRYPOINT ["python", "app.py"]
