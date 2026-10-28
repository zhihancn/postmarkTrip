#!/bin/sh
# =============================================================================
# postmarkTrip · 契约测试（黄金图像回归）
#
#   make test-contract          （或 sh infra/scripts/test-contract.sh）
#
# 这套测试要保证的事只有一件：
#   **同一个场景图 JSON，经不同语言的渲染实现，得到视觉一致的结果。**
#
# 这是 docs/ok/04 §3.3 的"契约优先于同语言"落地方式 ——
# 一致性由契约 + 可执行的回归测试保证，而不是由"同一个语言同一个库"保证，
# 这样前端（TypeScript/Konva）与服务端（Go）才能各自独立演化。
#
# 现状（v0.1）：还没有任何渲染实现，fixtures 与 golden 都不存在，
# 因此本脚本会明确报告 SKIP 而不是假装通过。
# =============================================================================
set -u

FIXTURE_DIR=tests/contract/fixtures
GOLDEN_DIR=tests/contract/golden

count() {
    # ls 失败时输出 0，避免 set -e 之外的意外
    if [ -d "$1" ]; then
        ls -1 "$1" 2>/dev/null | wc -l | tr -d ' '
    else
        printf '0'
    fi
}

n_fixtures=$(count "$FIXTURE_DIR")
n_golden=$(count "$GOLDEN_DIR")

printf '契约测试 · 黄金图像回归\n\n'
printf '  fixtures（场景图）: %s 个  [%s]\n' "$n_fixtures" "$FIXTURE_DIR"
printf '  golden（基准图）  : %s 张  [%s]\n' "$n_golden" "$GOLDEN_DIR"
printf '\n'

if [ "$n_fixtures" -eq 0 ] || [ "$n_golden" -eq 0 ]; then
    printf 'SKIP：契约测试尚未建立。\n\n'
    printf '  计划：\n'
    printf '    v1.0  定稿 contract/scene.schema.json，放入 %s/*.json\n' "$FIXTURE_DIR"
    printf '          与 %s/*.png（由前端渲染器生成并人工确认）\n' "$GOLDEN_DIR"
    printf '    v1.2  加入服务端渲染器后，**同一套测试自动**成为\n'
    printf '          "前端预览 vs 服务端导出"的对账机制，无需重写逻辑。\n'
    printf '\n'
    printf '  这也意味着现在没有可回归的对象 —— 不是失败，是尚未开始。\n'
    exit 0
fi

# -----------------------------------------------------------------------------
# 真正跑回归
#
# ⚠️ 当前 api 镜像**没有安装 [dev] extra**，因此容器内没有 pytest。
#    v1.0 接入契约测试时需要二选一：
#      a) 给 Dockerfile 加一个 dev 构建阶段（pip install -e '.[dev]'），或
#      b) 让 `make test-contract` 使用专门的测试镜像。
#    在那之前，下面这条命令会以"找不到 pytest"失败 —— 这是有意的显式失败，
#    好过用 `|| true` 把真实问题掩盖掉。
# -----------------------------------------------------------------------------
printf '运行回归（在 api 镜像内）…\n\n'
exec docker compose run --rm --no-deps api python -m pytest tests/contract -q
