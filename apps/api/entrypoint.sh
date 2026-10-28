#!/bin/sh
# =============================================================================
# postmarkTrip · API / Worker 容器入口
#
# 顺序（api 角色）：
#   1. 对齐绑定挂载目录的属主（PID 1 是 root，只做这一件事）
#   2. 等待 PostgreSQL 就绪
#   3. alembic upgrade head
#   4. 幂等初始化：管理员账号 + 预置标签
#   5. 降权到 pmtrip(10001) 并 exec uvicorn
#
# worker 角色跳过 3、4 —— 两个容器同时跑迁移会争抢 Alembic 版本表，
# 而且在 api 尚未建表时启动 worker 没有意义。
#
# 设计原则：出错时给出**可操作的下一步**，而不是只抛一段栈。
# =============================================================================
set -eu

# --------------------------------------------------------------------------- #
#  路径（容器内）
#  ASSETS_DIR 刻意不从环境读取：config.py 恒为 <DATA_DIR>/assets，
#  这里保持一致，避免 .env 里误设一个值造成两边不一致。
# --------------------------------------------------------------------------- #
DATA_DIR="${DATA_DIR:-/data}"
ASSETS_DIR="${DATA_DIR}/assets"
INBOX_DIR="${INBOX_DIR:-/inbox}"
PUBLIC_DATA_DIR="${PUBLIC_DATA_DIR:-/public-data}"
MODELS_DIR="${MODELS_DIR:-/models}"
APP_HOME="${APP_HOME:-/app/apps/api}"

#: 容器内 pmtrip 用户的 uid/gid（与 Dockerfile 保持一致）
PMTRIP_UID="${PMTRIP_UID:-10001}"
PMTRIP_GID="${PMTRIP_GID:-10001}"
#: 宿主用户的 gid。绑定挂载的宿主目录会设成这个组 + 组写 + setgid，
#: 这样**宿主用户与容器用户都能写**，且宿主目录的属主不被改变。
#: 0 = 未提供（宿主组不可确定），此时只加组写权限、不改属主也不改组。
HOST_GID="${HOST_GID:-0}"

DB_WAIT_TRIES="${DB_WAIT_TRIES:-60}"

# --------------------------------------------------------------------------- #
#  日志
# --------------------------------------------------------------------------- #
log() {
    printf '%s [entrypoint] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

warn() {
    printf '%s [entrypoint][警告] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
}

# --------------------------------------------------------------------------- #
#  权限
#
#  as_app / exec_as_app：以非 root 身份跑应用。
#  以 root 启动且 gosu 可用时降权；否则原样执行（并警告）。
# --------------------------------------------------------------------------- #
_have_gosu() {
    command -v gosu >/dev/null 2>&1
}

_should_drop() {
    [ "$(id -u)" = "0" ]
}

as_app() {
    if _should_drop; then
        if _have_gosu; then
            gosu pmtrip "$@"
        else
            warn "以 root 运行但找不到 gosu —— 应用进程将以 root 执行（不推荐）。"
            "$@"
        fi
    else
        "$@"
    fi
}

exec_as_app() {
    if _should_drop && _have_gosu; then
        exec gosu pmtrip "$@"
    fi
    exec "$@"
}

# --------------------------------------------------------------------------- #
#  目录准备
#
#  绑定挂载进来的宿主目录属于宿主上的那个用户，容器内的 pmtrip(10001) 可能
#  写不进去 —— 而 storage 层要在 assets 根目录建上传临时文件，健康检查也要
#  探测可写性。
#
#  ⚠️ 这里**不能**对绑定挂载的宿主目录 chown（以前就是这么做的，是个真缺陷）：
#     `chown pmtrip:pmtrip ./data/assets` 把宿主目录的属主改成 10001，
#     于是**宿主上的用户失去了写权限**，随之坏掉的是：
#       * `make backup` 生成资产清单（要遍历 data/assets）
#       * rsync / restic 之类的宿主侧备份
#       * 你手工往 assets 或 inbox 里放文件
#     而容器自己能写并不代表这样没问题 —— 宿主目录是**用户的东西**，
#     容器不该改变它的归属。
#
#  改成**加组写权限 + 设 setgid**：
#    * 容器的 pmtrip 同时属于宿主用户的 gid（见 Dockerfile 的 HOST_GID），
#      于是组写权限让它能写；
#    * setgid 让新建的子目录继承该组，mount 与后续文件不会跑回 10001 独占；
#    * 属主**保持不变**，宿主用户照旧可写。
#
#  不做递归：assets 下可能有数万个文件，递归会让每次启动都变慢。
#  已有的子目录属主可能是历史遗留的 10001（旧版本留下的），
#  那不影响使用 —— 宿主用户是**组**成员，组写权限足够。
# --------------------------------------------------------------------------- #
prepare_dirs() {
    if ! _should_drop; then
        return 0
    fi
    for d in "$DATA_DIR" "$ASSETS_DIR" "$INBOX_DIR" "$PUBLIC_DATA_DIR" "$MODELS_DIR"; do
        mkdir -p "$d" 2>/dev/null || true
    done

    # 用户会直接管理的两个目录：只加组写 + setgid，**不改属主**
    for d in "$ASSETS_DIR" "$INBOX_DIR"; do
        [ -d "$d" ] || continue
        chmod g+ws "$d" 2>/dev/null || true
        # HOST_GID 有效时把组对齐到宿主用户的组，让两边都能写
        if [ "$HOST_GID" != "0" ]; then
            chgrp "$HOST_GID" "$d" 2>/dev/null || true
        fi
    done

    # 纯容器内部的目录：属主对齐没有副作用，照旧
    for d in "$PUBLIC_DATA_DIR" "$MODELS_DIR"; do
        [ -d "$d" ] || continue
        chown pmtrip:pmtrip "$d" 2>/dev/null || true
    done
}

# --------------------------------------------------------------------------- #
#  等待数据库
#
#  刻意**不 import 应用包**：如果应用代码本身有问题，我们不希望它表现为
#  "数据库连不上"这种误导性的症状。这里只依赖 SQLAlchemy + DATABASE_URL。
# --------------------------------------------------------------------------- #
_db_probe() {
    as_app python -c '
import os
import sys

from sqlalchemy import create_engine, text

url = os.environ.get("DATABASE_URL") or (
    "postgresql+psycopg://postmarktrip:postmarktrip@db:5432/postmarktrip"
)
try:
    engine = create_engine(url, pool_pre_ping=True)
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
except Exception as exc:  # noqa: BLE001 - 探活失败的原因不重要，重试即可
    print(f"db probe failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    sys.exit(1)
'
}

wait_for_db() {
    log "等待数据库就绪（最多 ${DB_WAIT_TRIES} 次 × 2s）…"
    i=0
    # 把探活的 stderr 收进文件而不是直接吐到容器日志：
    # 60 次重试会把日志刷满，真正有用的反而被淹没。失败时再回放最后几行。
    probe_log="/tmp/pmtrip-db-probe.log"
    : > "$probe_log" 2>/dev/null || probe_log="/dev/null"

    while ! _db_probe 2>"$probe_log"; do
        i=$((i + 1))
        if [ "$i" -ge "$DB_WAIT_TRIES" ]; then
            log "数据库在 ${DB_WAIT_TRIES} 次重试后仍不可用，放弃启动。"
            if [ -s "$probe_log" ]; then
                log "最后一次探活报的错："
                tail -n 5 "$probe_log" | sed 's/^/    /'
            fi
            log "  排查：① docker compose logs db"
            log "        ② .env 里 DATABASE_URL 的库名/口令是否与"
            log "           POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB 一致"
            exit 1
        fi
        sleep 2
    done
    log "数据库已就绪。"
}

# --------------------------------------------------------------------------- #
#  迁移
# --------------------------------------------------------------------------- #
run_migrations() {
    log "执行数据库迁移：alembic upgrade head"
    if ! (cd "$APP_HOME" && as_app alembic upgrade head); then
        log "迁移失败。"
        log "  排查：① 数据库是否真就绪 ② DATABASE_URL 是否有误"
        log "        ③ migrations/versions 下的迁移脚本是否冲突"
        exit 1
    fi
    log "迁移完成。"
}

# --------------------------------------------------------------------------- #
#  初始化（幂等）
# --------------------------------------------------------------------------- #
run_bootstrap() {
    log "初始化管理员账号与预置标签（幂等，重复执行安全）…"
    if ! as_app python -m postmarktrip.bootstrap; then
        log "初始化失败。"
        log "  最常见原因：未设置 ADMIN_PASSWORD，或口令过短。"
        log "  请在 .env 中设置 ADMIN_USER / ADMIN_PASSWORD 后 docker compose up -d。"
        exit 1
    fi
    log "初始化完成。"
}

# --------------------------------------------------------------------------- #
#  启动 API
#
#  API_APP 可用环境变量覆盖，因此应用模块改名时**不需要重建镜像**。
# --------------------------------------------------------------------------- #
start_api() {
    app="${API_APP:-postmarktrip.main:app}"
    module="${app%%:*}"

    if ! as_app python -c 'import importlib, sys; importlib.import_module(sys.argv[1])' "$module" 2>/dev/null; then
        log "错误：找不到应用模块 '${module}'（API_APP=${app}）。"
        log "  uvicorn 需要一个形如 'postmarktrip.main:app' 的导入路径。"
        log "  若应用模块改名了，在 .env 里设置 API_APP 覆盖即可，无需重建镜像。"
        exit 1
    fi

    host="${API_HOST:-0.0.0.0}"
    port="${API_PORT:-8000}"
    level="$(printf '%s' "${LOG_LEVEL:-INFO}" | tr '[:upper:]' '[:lower:]')"

    # 用 set -- 拼参数，而不是拼字符串再 eval：既没有词分割风险，
    # 也不用为了 --reload 把整条命令写两遍。
    set -- uvicorn "$app" \
        --host "$host" \
        --port "$port" \
        --proxy-headers \
        --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}" \
        --no-server-header \
        --log-level "$level"

    # 开发模式（docker-compose.dev.yml 会设 API_RELOAD=1）。
    # --reload-dir 限定到源码目录：默认盯 CWD(/app)，会把 /data、/models
    # 这些数据目录也纳入监视，白白消耗 inotify 配额。
    if [ "${API_RELOAD:-0}" = "1" ]; then
        set -- "$@" --reload --reload-dir "${API_RELOAD_DIR:-${APP_HOME}/src}"
        log "热重载已启用（API_RELOAD=1，监视 ${API_RELOAD_DIR:-${APP_HOME}/src}）"
    fi

    log "启动 API：uvicorn ${app} --host ${host} --port ${port}（日志级别 ${level}）"
    # --proxy-headers + --forwarded-allow-ips：gateway(nginx) 在前面，
    # 没有它们应用看到的客户端 IP 永远是网关的地址。
    exec_as_app "$@"
}

# --------------------------------------------------------------------------- #
#  启动 worker
#
#  ARQ 的 WorkerSettings 尚未实现（v1.0 启用）。此时**不能退出**：
#  restart: unless-stopped 会把容器变成重启风暴，日志被刷屏且 ps 状态难以解读。
#  改为待机并明确说明原因。
# --------------------------------------------------------------------------- #
start_worker() {
    if as_app python -c 'import importlib; importlib.import_module("postmarktrip.worker.settings")' 2>/dev/null; then
        log "启动 ARQ worker：postmarktrip.worker.settings.WorkerSettings"
        exec_as_app arq postmarktrip.worker.settings.WorkerSettings
    fi

    log "worker 模块（postmarktrip.worker.settings）尚未实现 —— v1.0 启用。"
    log "本容器进入待机（sleep infinity）：不退出，因此不会形成重启风暴。"
    log "待机期间仍以 Redis 连通性作为健康检查依据（见 docker-compose.yml）。"
    exec_as_app sleep infinity
}

# --------------------------------------------------------------------------- #
#  主流程
# --------------------------------------------------------------------------- #
main() {
    case "${1:-api}" in
        api)
            log "postmarkTrip 启动 —— 角色=api，TZ=${TZ:-UTC}"
            prepare_dirs
            wait_for_db
            if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
                run_migrations
                run_bootstrap
            else
                log "RUN_MIGRATIONS=0 —— 跳过迁移与初始化（多副本部署时只让一个实例执行）。"
            fi
            start_api
            ;;
        worker)
            log "postmarkTrip 启动 —— 角色=worker，TZ=${TZ:-UTC}"
            prepare_dirs
            wait_for_db
            start_worker
            ;;
        *)
            # 允许 docker compose run api bash 这类直接执行命令的用法。
            log "未识别为角色参数（'${1}'），按原样执行该命令。"
            prepare_dirs
            exec_as_app "$@"
            ;;
    esac
}

main "$@"
