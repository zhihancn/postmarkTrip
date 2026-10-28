#!/bin/sh
# =============================================================================
# postmarkTrip · 契约层校验
#
#   make gen-contract          （或 sh infra/scripts/check-contract.sh）
#
# 契约层（contract/）是"模板"的唯一定义：卡片 = 一份场景图 JSON，
# 前端渲染器与服务端渲染器都消费它（见 docs/ok/04 §3.3）。
# 它必须始终是合法 JSON —— 一旦坏掉，三个语言侧的类型生成全会失败。
#
# 刻意在 **api 容器里**跑校验，而不是要求宿主机装 Python：
# 这是"宿主机只需要 Docker + 编辑器"这条约定的直接体现。
#
# 至于把 schema 生成为 types.ts / types.py / types.go：
# 那一步要在 v1.0 定稿 schema 之后接入（见 docs/spec/项目骨架与命名规范.md §3.2）。
# =============================================================================
set -u

if [ ! -f .env ]; then
    printf '提示：没有 .env，改用 docker run 直接校验（不需要数据库）。\n\n'
fi

printf '契约层文件：\n'
ls -1 contract/*.schema.json 2>/dev/null | sed 's/^/  /' || printf '  （没有找到 contract/*.schema.json）\n'
printf '\n'

# --entrypoint python：绕过 api 镜像的 entrypoint（不需要等库、迁移、初始化）。
# --no-deps：不拉起 db / redis，这是个纯静态校验。
docker compose run --rm --no-deps --entrypoint python api -c '
import glob
import json
import sys

files = sorted(glob.glob("/app/contract/*.schema.json"))
if not files:
    print("  （容器内没有找到 /app/contract/*.schema.json）")
    print("  检查 docker-compose.yml 里 api 的构建上下文是否为仓库根目录。")
    sys.exit(1)

bad = 0
for path in files:
    try:
        with open(path, encoding="utf-8") as fh:
            json.load(fh)
        print(f"  OK   {path}")
    except Exception as exc:
        print(f"  BAD  {path}")
        print(f"       {type(exc).__name__}: {exc}")
        bad = 1

print()
if bad:
    print("契约层存在无效 JSON，请先修好再继续。")
else:
    print("全部为合法 JSON。")
print("类型代码生成（contract/gen/types.{ts,py,go}）计划在 v1.0 接入。")
sys.exit(bad)
'
