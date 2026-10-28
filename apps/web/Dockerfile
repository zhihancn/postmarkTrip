# syntax=docker/dockerfile:1.7
# =============================================================================
# postmarkTrip · 前端 + 网关镜像
#
# 这是 **合并式拓扑**（docker-compose.yml 的 gateway 服务）：
# 静态前端产物与 nginx 网关在同一个容器里，容器内监听 80，
# 由 compose 映射到宿主的 ${APP_PORT}（默认 9999）。
#
# -----------------------------------------------------------------------------
# ⚠️ 两条容易踩的约定
#
# 1. **构建上下文是仓库根目录**（docker-compose.yml 里写的是 `context: .`），
#    因为要能取到 infra/ 与 contract/。所以下面每一处 COPY 的源路径都带
#    `apps/web/` 前缀 —— 不要改成相对 apps/web 的写法，那样 compose 构建会全部失败。
#    这里刻意用**写死的路径**而不是 `ARG WEB_DIR`：构建上下文是部署契约的一部分，
#    把它参数化只会让"构建失败"变得更难排查，而这条路径没有第二个调用方。
#
# 2. **网关配置取自 `infra/gateway/nginx.conf`**，它是唯一真源。
#    那份配置被两种拓扑共用（理由见 infra/gateway/Dockerfile 的注释）：
#      * 合并式：本文件 → 静态产物 + 网关同容器
#      * 分离式：infra/gateway/Dockerfile → 纯网关，静态文件外部挂载
#    它在文件顶层定义了 `map $http_upgrade $connection_upgrade`，并对外提供
#    `/gateway-health` —— 后者是 infra/scripts/doctor.sh 用来区分
#    "网关活着但后端挂了" 与 "整个容器没起来" 的依据，不能丢。
#
#    这里曾经还有第二份 apps/web/nginx.conf，**已删除**：两份网关配置必然漂移，
#    而"契约只有一个真源"是本项目的基本原则（同 contract/ 的理由）。
# =============================================================================

# ---------------------------------------------------------------------------
# 构建阶段
# ---------------------------------------------------------------------------
FROM node:22-alpine AS build

WORKDIR /app

# 先只拷依赖清单 —— 源码改动不会让依赖层缓存失效
COPY apps/web/package.json ./package.json
RUN npm install --no-audit --no-fund

# 再拷源码
COPY apps/web/tsconfig.json apps/web/vite.config.ts apps/web/index.html ./
COPY apps/web/public ./public
COPY apps/web/src ./src

# 这里刻意**不跑 tsc**：类型检查交给 `npm run typecheck` 与 CI，
# 不让一个类型错误把整个镜像构建卡死。想两者都跑用 `npm run build:check`。
RUN npm run build

# ---------------------------------------------------------------------------
# 运行阶段
# ---------------------------------------------------------------------------
FROM nginx:alpine

ENV TZ=Asia/Shanghai

# tzdata：alpine 基础镜像不带时区库，没有它 nginx 日志时间戳会是 UTC。
RUN set -eux; \
    apk add --no-cache tzdata; \
    cp "/usr/share/zoneinfo/${TZ}" /etc/localtime; \
    echo "${TZ}" > /etc/timezone

# 网关配置（见文件头第 2 条）。覆盖 alpine 镜像自带的 default.conf。
#
# 与 infra/gateway/Dockerfile 一致，这里**不执行 `nginx -t`** 做构建期校验：
# nginx 解析配置时会去解析 `proxy_pass http://api:8000` 里的主机名，而构建
# 容器里没有 `api` 这个 DNS 名，`nginx -t` 必然以 host not found 失败 ——
# 那是假阳性，不是配置错误。语法校验放在运行时：
#     docker compose exec gateway nginx -t
COPY infra/gateway/nginx.conf /etc/nginx/conf.d/default.conf

# 构建产物
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

# 打网关自己的端点（不是 /api/health）—— 这样"网关活着、后端挂了"
# 仍然报告为健康，运维据此继续往下查。
# 用 busybox 自带的 wget，不额外装 curl。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1/gateway-health || exit 1

CMD ["nginx", "-g", "daemon off;"]
