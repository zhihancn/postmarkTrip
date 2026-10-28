# syntax=docker/dockerfile:1.7
# =============================================================================
# postmarkTrip · gateway-only 镜像（**分离式拓扑**用）
#
# 默认拓扑是"合并式"：前端镜像（apps/web/Dockerfile，由前端负责人维护）里
# COPY 一份 infra/gateway/nginx.conf 到 /etc/nginx/conf.d/default.conf，
# 静态产物和网关在同一个容器里。
#
# 本镜像服务另一种拓扑：网关与前端分开 —— 网关只做反代，静态文件由外部提供
# （命名卷、宿主绑定挂载、或另一台机器上的静态站点）。
# 用法见 infra/compose/gateway-standalone.yml。
#
# 构建（上下文是仓库根目录）：
#     docker build -f infra/gateway/Dockerfile -t postmarktrip/gateway:dev .
# =============================================================================

FROM nginx:1.27-alpine

ENV TZ=Asia/Shanghai

# tzdata：让 nginx 的日志时间戳落在本地时区（alpine 基础镜像不带时区库）。
# curl：仅用于容器内自检，不参与对外服务。
RUN set -eux; \
    apk add --no-cache tzdata curl; \
    cp "/usr/share/zoneinfo/${TZ}" /etc/localtime; \
    echo "${TZ}" > /etc/timezone

# 覆盖 alpine nginx 镜像自带的 default.conf。
#
# 这里**刻意不执行 `nginx -t`** 做构建期校验：nginx 会在加载配置时解析
# `proxy_pass http://api:8000` 里的主机名，而构建容器里没有 `api` 这个 DNS 名，
# `nginx -t` 必然以 "host not found in upstream" 失败 —— 那是假阳性，不是配置错误。
# 语法校验放在运行时：`docker compose exec gateway nginx -t`（doctor 会做）。
COPY infra/gateway/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

# 健康检查用 gateway 自己的端点：它与 /api/health 分开，正是为了让"网关活着、
# 后端挂了"与"整个容器没起来"两种情况可区分。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1/gateway-health || exit 1
