# =========================================================
# AI NewsDesk · 生产镜像（Render / Hugging Face Spaces / 任意 Docker 平台通用）
#
# 特点：
#   - 不打包 .env（配置全部走平台环境变量，见 docs/部署教程.md）
#   - 演示数据库 data/ai_news.db 随镜像一起发布，首次访问即有内容
#   - 非 root 用户运行（Hugging Face Spaces 等平台更友好）
#   - 监听端口取 $PORT（Render 会注入），未注入时默认 8000
# =========================================================
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_HOME=/app \
    TZ=Asia/Shanghai

WORKDIR ${APP_HOME}

# 系统依赖：curl 用于容器健康检查，tzdata 保证时区数据完整
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖，利用 Docker 层缓存：只改代码时不会重复 pip install
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 再拷业务代码（.env / logs / .git 已在 .dockerignore 中排除）
COPY . .

# 非 root 运行
RUN useradd -m -u 1000 appuser \
    && chown -R appuser:appuser ${APP_HOME}
USER appuser

EXPOSE 8000

# /login 不需要登录即可访问，适合做健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/login" || exit 1

# shell 形式以支持 ${PORT} 变量展开
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
