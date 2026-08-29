# ============================================================
# VeroRun — 独立部署 Docker 镜像
# 单容器运行所有服务（platform/admin/auth-center/captcha）
# ============================================================
FROM python:3.12-slim-bookworm AS builder

WORKDIR /app

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    nginx-light supervisor curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY requirements.txt requirements.lock /app/
# 审计 M16：安装带哈希锁定的依赖，构建可复现、防上游投毒
RUN pip install --no-cache-dir -r requirements.lock

# ── 运行时镜像 ──
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    nginx-light supervisor curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 从 builder 复制 Python 包
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# 复制项目代码（排除不需要的文件）
COPY . /app/
# 审计 C-1 修复：不再使用 /app/.* 通配（会匹配所有以点开头的隐藏文件，
# 存在误删 .env/.gitignore 等风险），改为精确 find 删除缓存与版本控制目录。
RUN find /app -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null; \
    find /app -name '*.pyc' -delete; \
    find /app -name '.git' -type d -exec rm -rf {} + 2>/dev/null; \
    find /app -name '.github' -type d -exec rm -rf {} + 2>/dev/null

# Nginx 配置
COPY deploy/nginx.conf /etc/nginx/sites-enabled/default

# Supervisor 配置
COPY deploy/supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# 入口脚本
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 数据卷
VOLUME ["/app/data"]

# 审计 M-4 修复：创建非 root 运行用户。
# gunicorn 服务以 verorun 用户运行（最小权限原则）；nginx 保留 root 绑定 80 端口。
RUN useradd -m -s /bin/bash verorun

# 审计 D8：挂载卷 /app/data 属主修正为 verorun。
# 未修正时，挂载宿主目录后属主随机，gunicorn 无法写入导致启动失败。
RUN chown -R verorun:verorun /app/data

EXPOSE 80

# 审计 M13：容器级健康检查（探活 health-service，不依赖编排层额外配置）
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8085/health >/dev/null || exit 1

ENTRYPOINT ["/entrypoint.sh"]
