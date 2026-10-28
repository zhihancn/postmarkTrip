#!/bin/sh
# =============================================================================
# postmarkTrip · 自检（Linux / macOS / WSL）
#
#   make doctor          （或直接 sh infra/scripts/doctor.sh）
#
# Windows 上由 pmtrip.ps1 的 doctor 子命令实现**同一套检查**。
# ⚠️ 两个实现必须保持同步 —— 增删检查项时请同时改 pmtrip.ps1。
#
# 设计原则：运维最怕"不知道哪里不对"。所以这里逐项打印 ✓/✗/!，
# 并且**每一项失败都给出可执行的下一步**，而不是只丢一句报错。
#
# 退出码：0 = 无失败项（允许有警告）；1 = 存在失败项。
# =============================================================================
set -u

# 颜色（非终端时自动关闭，便于被脚本采集）
if [ -t 1 ]; then
    C_OK=$(printf '\033[32m'); C_BAD=$(printf '\033[31m')
    C_WARN=$(printf '\033[33m'); C_DIM=$(printf '\033[2m')
    C_OFF=$(printf '\033[0m')
else
    C_OK=; C_BAD=; C_WARN=; C_DIM=; C_OFF=
fi

# 计数器必须只在**当前 shell** 里累加 —— 注意不要放进管道右侧的子 shell。
FAILED=0
WARNED=0

ok()    { printf '  %s✓%s %s\n' "$C_OK" "$C_OFF" "$1"; }
bad()   { printf '  %s✗%s %s\n' "$C_BAD" "$C_OFF" "$1"; FAILED=$((FAILED + 1)); }
warn()  { printf '  %s!%s %s\n' "$C_WARN" "$C_OFF" "$1"; WARNED=$((WARNED + 1)); }
hint()  { printf '      %s-> %s%s\n' "$C_DIM" "$1" "$C_OFF"; }
section() { printf '\n%s\n' "$1"; }

# 提示（不计入警告数）。
#
# 与 warn 的区别：warn 是"有东西不对，去修"；note 是"给你个建议"。
# 两者的计数必须分开 —— 一条**无法清除**的 warn 会让 WARNED 永远不为 0，
# 于是末尾那句"全部通过。"永远打印不出来，而且会稀释真正警告的信号。
note()  { printf '  %s·%s %s\n' "$C_DIM" "$C_OFF" "$1"; }

# 从 .env 读一个值。
# 刻意**不 source** 整个 .env：文件里可能有中文注释或特殊字符，source 会把脚本带崩。
env_get() {
    [ -f .env ] || return 0
    grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2-
}

printf '%s\n' "postmarkTrip · 自检"
printf '目录：%s\n' "$(pwd)"

# --------------------------------------------------------------------------- #
section "1/9 Docker 引擎"
if command -v docker >/dev/null 2>&1; then
    _ver=$(docker version --format '{{.Server.Version}}' 2>/dev/null)
    if [ -n "$_ver" ]; then
        ok "docker 引擎运行中（Server ${_ver}）"
    else
        bad "docker 命令存在，但连不上引擎"
        hint "Windows/macOS：启动 Docker Desktop 并等它显示 Running"
        hint "Linux：sudo systemctl start docker"
    fi
else
    bad "找不到 docker 命令"
    hint "安装 Docker Desktop（Windows/macOS），或 docker-ce + docker-compose-plugin（Linux）"
fi

# --------------------------------------------------------------------------- #
section "2/9 Docker Compose"
_cv=$(docker compose version --short 2>/dev/null)
if [ -n "$_cv" ]; then
    ok "docker compose ${_cv}"
else
    bad "docker compose 不可用（Compose v2 是 docker 的子命令，不是独立的 docker-compose）"
    hint "Linux：安装 docker-compose-plugin 包"
fi

# --------------------------------------------------------------------------- #
section "3/9 配置文件 .env"
if [ -f .env ]; then
    ok ".env 存在"
else
    bad "缺少 .env"
    hint "cp .env.example .env"
    hint "然后至少改 SECRET_KEY / ADMIN_PASSWORD / POSTGRES_PASSWORD"
fi

# --------------------------------------------------------------------------- #
section "4/9 示例值是否还没改"
check_placeholder() {
    _name=$1
    _why=$2
    _val=$(env_get "$_name")
    if [ -z "$_val" ]; then
        warn "${_name} 为空"
        hint "$_why"
    elif printf '%s' "$_val" | grep -q '^CHANGE_ME'; then
        warn "${_name} 仍是 .env.example 里的示例值"
        hint "$_why"
    else
        ok "${_name} 已设置"
    fi
}
check_placeholder SECRET_KEY "openssl rand -base64 48（改了它所有会话失效，需重新登录）"
check_placeholder ADMIN_PASSWORD "设一个至少 8 位的强口令"
check_placeholder POSTGRES_PASSWORD "改完记得同步改 DATABASE_URL 里那一段密码"

# DATABASE_URL 与 POSTGRES_PASSWORD 的一致性 —— 最常见的"连不上库"
_db_url=$(env_get DATABASE_URL)
_pg_pw=$(env_get POSTGRES_PASSWORD)
if [ -n "$_db_url" ] && [ -n "$_pg_pw" ]; then
    case "$_db_url" in
        *":$_pg_pw@"*) ok "DATABASE_URL 里的口令与 POSTGRES_PASSWORD 一致" ;;
        *) bad "DATABASE_URL 里的口令与 POSTGRES_PASSWORD 不一致"
           hint "两者必须是同一串；改了口令请同步改 DATABASE_URL" ;;
    esac
fi

# --------------------------------------------------------------------------- #
section "5/9 存储（宿主目录 + 命名卷）"
ensure_dir() {
    if [ -d "$1" ]; then
        ok "$1 存在"
    elif mkdir -p "$1" 2>/dev/null; then
        ok "$1 已创建"
    else
        bad "$1 不存在且无法创建"
        hint "检查当前用户对 $(dirname "$1") 的写权限"
    fi
}
# 只有这些是宿主目录。数据库与 Redis 用命名卷（见 docker-compose.yml 的"存储策略"）。
for d in data data/assets inbox public-data models backups/db; do
    ensure_dir "$d"
done

if [ -d data/assets ] && [ -w data/assets ]; then
    ok "data/assets 可写（storage 层要在这里建上传临时文件）"
else
    bad "data/assets 不可写"
    hint "容器内 pmtrip(10001) 需要写权限；entrypoint 会对齐顶层属主，但宿主目录本身必须可写"
fi

# 存储位置：数据库是**绑定挂载**，Redis 是命名卷。
#
# ⚠️ 数据库为什么不是命名卷（2026-10-11 改，原因是踩出来的）：
# 这台机器上反复出现 `docker compose down -v`，而 `-v` **只删本项目声明的命名卷** ——
# 数据库因此被反复清空（一天 8 次以上）。改成绑定挂载后 `down -v` 碰不到它。
# 详见 docker-compose.yml 里 db.volumes 上的注释与 ok/09 E-06。
#
# 运维最常问的就是"我的数据到底在哪"，所以这里把真实位置打出来。
_db_src="${PMTRIP_PGDATA:-$HOME/pmtrip-pgdata}"
if [ -d "$_db_src" ]; then
    ok "数据库（绑定挂载）${_db_src}"
    printf '      %s%s%s\n' "$C_DIM" "$(du -sh "$_db_src" 2>/dev/null | cut -f1 || echo '?')" "$C_OFF"
else
    warn "数据库目录 ${_db_src} 还不存在"
    hint "首次 make up 时若报错，先 mkdir -p ${_db_src}（见 _setup_pgdata.sh）"
fi

for vol in postmarktrip-redis; do
    _mp=$(docker volume inspect --format '{{.Mountpoint}}' "$vol" 2>/dev/null)
    if [ -n "$_mp" ]; then
        ok "卷 ${vol}"
        printf '      %s%s%s\n' "$C_DIM" "$_mp" "$C_OFF"
    else
        warn "卷 ${vol} 还不存在"
        hint "首次 make up 时自动创建。想确认路径：docker volume inspect ${vol}"
    fi
done
hint "数据库目录/卷里是原始文件 —— **不要手工改动**。备份请用 make backup（pg_dump）"

# --------------------------------------------------------------------------- #
section "6/9 容器状态"
# 用 here-doc 而不是管道：管道右侧是子 shell，bad()/warn() 的计数会丢。
_ps=$(docker compose ps --format '{{.Service}}|{{.State}}|{{.Health}}' 2>/dev/null)
if [ -n "$_ps" ]; then
    while IFS='|' read -r svc state health; do
        [ -n "$svc" ] || continue
        case "${state}/${health}" in
            running/healthy)    ok "${svc}：running / healthy" ;;
            running/running)    ok "${svc}：running（该服务无健康检查）" ;;
            running/|running)   ok "${svc}：running" ;;
            running/unhealthy)  bad "${svc}：running / unhealthy"
                                hint "docker compose logs --tail=50 ${svc}" ;;
            *)                  bad "${svc}：${state}${health:+/$health}"
                                hint "docker compose logs --tail=50 ${svc}" ;;
        esac
    done <<EOF
$_ps
EOF
else
    warn "没有正在运行的容器（或无法查询状态）"
    hint "make up"
fi

# --------------------------------------------------------------------------- #
section "7/9 HTTP 端点"
_port=$(env_get APP_PORT)
_port=${_port:-9999}
_base="http://127.0.0.1:${_port}"

# curl 优先，退回 wget；两者都没有返回 127
http_get() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time 5 "$1" 2>/dev/null
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O - --timeout=5 "$1" 2>/dev/null
    else
        return 127
    fi
}

# 7a. 网关自身 —— 与 /api/health 分开，用于区分"网关活着但后端挂了"
_gw=$(http_get "${_base}/gateway-health")
_gw_rc=$?
case "$_gw_rc" in
    0)
        if [ -n "$_gw" ]; then
            ok "gateway 有响应：${_base}/gateway-health"
        else
            bad "gateway 返回空响应"
            hint "docker compose logs --tail=50 gateway"
        fi
        ;;
    127) warn "宿主没有 curl 也没有 wget，跳过 HTTP 检查" ;;
    *)
        bad "网关无响应：${_base}/gateway-health"
        hint "docker compose ps 看 gateway 是否 healthy"
        hint "若 gateway 一直是 created（没启动），多半是 api 没变 healthy —— gateway 依赖它"
        hint "docker compose logs --tail=50 api"
        ;;
esac

# 7b. 应用健康 —— 逐项展开，出问题时能直接定位到是哪一环
if [ "$_gw_rc" != "127" ]; then
    _health=$(http_get "${_base}/api/health")
    _h_rc=$?
    if [ "$_h_rc" != "0" ] || [ -z "$_health" ]; then
        bad "api 无响应：${_base}/api/health"
        hint "docker compose logs --tail=50 api"
    else
        if printf '%s' "$_health" | grep -qE '"ok"[[:space:]]*:[[:space:]]*true'; then
            ok "api /api/health ok=true"
        else
            bad "api /api/health ok=false（下面逐项看是哪一环）"
        fi
        for k in database assets_writable inbox_present; do
            if printf '%s' "$_health" | grep -qE "\"${k}\"[[:space:]]*:[[:space:]]*true"; then
                ok "${k} = true"
            else
                bad "${k} = false"
                case "$k" in
                    database)        hint "docker compose logs db；核对 DATABASE_URL 与 POSTGRES_*" ;;
                    assets_writable) hint "宿主 data/assets 写权限，或 entrypoint 的 chown 失败" ;;
                    inbox_present)   hint "宿主 inbox/ 是否挂进了容器" ;;
                esac
            fi
        done
        _ocr=$(printf '%s' "$_health" \
            | sed -n 's/.*"ocr_engine"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
        if [ -n "$_ocr" ]; then
            ok "ocr_engine = ${_ocr}（v0.1 为 none，属正常）"
        fi

        # --- worker 存活 ---------------------------------------------------- #
        # /api/health 的 worker_alive 是**三态**：true / false / null。
        # null 表示"查不到"（Redis 不可达），与 false（worker 没心跳）不同。
        # 这个检查是新增的：此前 worker 崩溃-重启 219 次，而 compose 的
        # healthcheck 只 ping Redis，于是它一直显示 healthy（见 worker/tasks.py）。
        if printf '%s' "$_health" | grep -qE '"worker_alive"[[:space:]]*:[[:space:]]*true'; then
            ok "worker 心跳新鲜（ARQ 队列在转）"
        elif printf '%s' "$_health" | grep -qE '"worker_alive"[[:space:]]*:[[:space:]]*false'; then
            bad "worker 没有心跳 —— 队列里的任务不会被处理"
            hint "docker compose logs --tail=50 worker"
            hint "worker 崩过 219 次却显示 healthy 是本项目已修过的缺陷：健康检查曾只 ping Redis"
        else
            warn "worker 存活状态未知（Redis 不可达？）"
            hint "docker compose ps 看 redis 是否 healthy"
        fi

        # --- 识别 Provider -------------------------------------------------- #
        # 读探测出来的真实结果，而不是配置里的意图。
        if printf '%s' "$_health" | grep -qE '"cv_ready"[[:space:]]*:[[:space:]]*true'; then
            ok "CV 就绪（OpenCV 可导入）—— 切分/戳型判别可用"
        else
            warn "CV 未就绪（未装 recognition extra）"
            hint "不需要识别时可忽略；要开发识别管线：docker compose build --build-arg INSTALL_RECOGNITION=1 api"
        fi
        _blocked=$(printf '%s' "$_health" \
            | sed -n 's/.*"blocked"[[:space:]]*:[[:space:]]*\[\([^]]*\)\].*/\1/p')
        if [ -n "$_blocked" ]; then
            warn "已启用但不可用的 Provider：${_blocked}"
            hint "装了但用不了是最该处理的状态（配置写了、依赖没装）"
        fi
    fi
fi

# --------------------------------------------------------------------------- #
section "8/9 端口 ${_port}"
if command -v ss >/dev/null 2>&1; then
    _listen=$(ss -ltn 2>/dev/null | grep -E "[:.]${_port}[[:space:]]" || true)
elif command -v lsof >/dev/null 2>&1; then
    _listen=$(lsof -nP -iTCP:"${_port}" -sTCP:LISTEN 2>/dev/null || true)
elif command -v netstat >/dev/null 2>&1; then
    _listen=$(netstat -ltn 2>/dev/null | grep -E "[:.]${_port}[[:space:]]" || true)
else
    _listen=""
fi
if [ -n "$_listen" ]; then
    ok "端口 ${_port} 正在监听"
else
    warn "端口 ${_port} 没有监听"
    hint "容器没起来，或 .env 里的 APP_PORT 是别的值"
fi

# --------------------------------------------------------------------------- #
section "9/9 备份"
_last_dump=$(ls -1t backups/db/*.dump 2>/dev/null | head -1 || true)
if [ -n "$_last_dump" ]; then
    ok "最近一次数据库转储：${_last_dump}"
else
    warn "还没有任何数据库转储"
    hint "make backup"
    # 这一条是**经历真实数据丢失之后加的**：数据库在 Docker 命名卷里，
    # `docker compose down -v` / 手工删卷 / Docker Desktop 清理都会让它消失，
    # 而"从未备份过"意味着那时**没有任何退路**。
    # 原先只提示 make backup，没有说清后果，于是它被当成一条普通提醒跳过了。
    hint "⚠️ 命名卷被删（down -v / 清理卷）就会让**全部记录**消失，且无法恢复"
    hint "建议现在就定个自动化：cron 调 make backup（见 docs/ok/07 §八）"
fi
if [ -f .env ]; then
    # note 而不是 warn：这是**建议**，不是缺陷。用 warn 会让 WARNED 永远 >= 1，
    # 于是末尾那句"全部通过。"成了死代码。
    note ".env 刻意不在自动备份范围内（里面有密钥）"
    hint "请**单独**把它抄一份到安全的地方 —— 丢了 SECRET_KEY，所有人都要重新登录"
fi

# --------------------------------------------------------------------------- #
printf '\n%s\n' "----------------------------------------"
if [ "$FAILED" -eq 0 ] && [ "$WARNED" -eq 0 ]; then
    printf '%s全部通过。%s\n' "$C_OK" "$C_OFF"
elif [ "$FAILED" -eq 0 ]; then
    printf '%s通过，但有 %d 项警告。%s\n' "$C_WARN" "$WARNED" "$C_OFF"
else
    printf '%s失败 %d 项，警告 %d 项。%s\n' "$C_BAD" "$FAILED" "$WARNED" "$C_OFF"
fi

[ "$FAILED" -eq 0 ] || exit 1
exit 0
