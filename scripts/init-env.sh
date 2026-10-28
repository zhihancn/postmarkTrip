#!/bin/sh
# =============================================================================
# 首次启动时生成 .env（随机密钥与口令）。
#
# 由 `make up` 调用；Windows 上 pmtrip.ps1 里有等价实现，两边输出一致。
#
# 为什么不交给网页：容器启动**之前**没有网页，而 SECRET_KEY /
# POSTGRES_PASSWORD 在 compose 解析阶段就必需。浏览器能收集的只有
# 登录之后的运行时设置（VLM / Colnect Key）。
# =============================================================================
set -eu

cd "$(dirname "$0")/.." || exit 1

if [ -f .env ]; then
    exit 0
fi

if [ ! -f .env.example ]; then
    printf '错误：找不到 .env.example，无法生成 .env\n' >&2
    exit 1
fi

random_b64() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -base64 "$1" | tr -d '\n'
    else
        head -c "$1" /dev/urandom | base64 | tr -d '\n'
    fi
}

random_alnum() {
    # 去掉 0/O/1/l/I 之类的易混字符：这个口令要被人从终端抄到浏览器里
    random_b64 32 | tr -dc 'A-Za-z0-9' | cut -c1-"$1"
}

secret=$(random_b64 48)
pg_pass=$(random_alnum 24)
admin_pass=$(random_alnum 16)

umask 077

awk -v s="$secret" -v p="$pg_pass" -v a="$admin_pass" '
    /^SECRET_KEY=/        { print "SECRET_KEY=" s; next }
    /^POSTGRES_PASSWORD=/ { print "POSTGRES_PASSWORD=" p; next }
    /^ADMIN_PASSWORD=/    { print "ADMIN_PASSWORD=" a; next }
    /^DATABASE_URL=/      { print "DATABASE_URL=postgresql+psycopg://postmarktrip:" p "@db:5432/postmarktrip"; next }
    { print }
' .env.example > .env

chmod 600 .env 2>/dev/null || true

mkdir -p data
cat > data/initial-credentials.txt <<EOF
postmarkTrip 首次启动生成的凭据
用户名: admin
口令:   $admin_pass

登录后请立刻修改口令，并删除本文件。
EOF
chmod 600 data/initial-credentials.txt 2>/dev/null || true

printf '\n未发现 .env，已生成一份（随机密钥与口令）：\n\n'
printf '  管理员账号：admin\n'
printf '  管理员口令：%s\n\n' "$admin_pass"
printf '  同时写入：data/initial-credentials.txt（登录后请删除）\n'
printf '  如需从局域网其他设备访问，改 .env 的 APP_HOST_BIND。\n\n'
