#!/usr/bin/env python3
"""把民政部的行政区划代码表灌进 ``place`` 表。

## 数据源（用户 2026-10-08 选定）

**民政部《XXXX 年中华人民共和国县以上行政区划代码》**，从
``https://www.mca.gov.cn/mzsj/xzqh/`` 下载后**另存为 .html**（Excel 导出的
HTML 表格）。实测 2024 年那份有 **3213 行**：省级 34 / 地级 333 / 县级 2846。

不采用国家统计局的《统计用区划代码》：那一页从本机返回 **403**（反爬），
而且它才有乡/村级 —— 用户确认本轮**只灌到县级**。

## 这份 HTML 的五个坑（都是实测撞出来的）

| # | 坑 | 处理 |
| --- | --- | --- |
| 1 | `name` 混着 `&nbsp;` 与前导缩进空格 | 先解实体再折叠空白 |
| 2 | **32 个名字带 `*`**（`济源市*`）—— 民政部标注的"省直辖" | 去掉 `*`；它们**仍是县级**，父为省级 |
| 3 | **父子不能纯靠码前缀推** —— `110101 东城区` 的"前 4 位 `110100`"不存在（直辖市中间层留空） | **按层级逐级上溯**。这类共 118 个 |
| 4 | 自治单位的短名规则在 `vocab.place_short_name` 里 | 本脚本只存**标准全名**，短名由词表运行时产出 |
| 5 | `place` 表**只有主键约束**（没有 admin_code / path 唯一键） | 幂等**必须靠本脚本自己判存在**，不能指望数据库拦 |

## 用法

与本目录既有工具同一个约定（`sys.path.insert(parents[2]/"src")`，直接跑脚本）：

```bash
cd apps/api
python tools/import_places.py /path/to/民政部行政区划代码.html --dry-run   # 先看报告
python tools/import_places.py /path/to/民政部行政区划代码.html             # 真写（幂等）
```

⚠️ **不要在容器外跑它去连容器里的库**（除非把 `DATABASE_URL` 指对）。
最省事的用法是拿进 api 容器里跑 —— 那里 `DATABASE_URL` 已经对了。
仓库根的 `_import_places.sh` 把"拷 HTML + 拷脚本 + 跑"这三步包好了。
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: 与 `tools/samples/` 里既有的工具同一个约定：把 apps/api/src 挂上，
#: 这样 `import postmarktrip...` 能解析（脚本是**直接跑文件**，不是 -m）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# --------------------------------------------------------------------------- #
#  常量：**不要**把层级数写死
#
#  place 表的设计就是"不硬编码层级"（models.py 明写），所以这里用
#  "代码形状 → 层级"的规则，而不是"省级/地级/县级"三个固定名字。
#  将来灌乡/村级（9 位/12 位码）时，只需在 LEVELS 里加两条，其余代码不动。
# --------------------------------------------------------------------------- #

#: 国家节点的名字与代码。数据里没有这一行，由本脚本补 —— 因为
#: `place.parent_id` 是自引用，树需要一个根；而且词表的层级链从它开始。
COUNTRY_NAME = "中华人民共和国"
COUNTRY_CODE = "CN"


@dataclass(frozen=True, slots=True)
class Level:
    """一个层级：代码形状 + 名称 + 代码长度。"""

    suffix_pattern: str
    label: str
    code_len: int
    #: 期望的节点数量级（用于自检报警，不是硬断言）
    expect_about: int | None = None


#: 从**深到浅**排列，匹配时先试深的（12 位码不会与 6 位码混淆，但顺序更清楚）
LEVELS: tuple[Level, ...] = (
    Level(r"\d{2}0000$", "省级", 6, 34),
    Level(r"\d{4}00$", "地级市", 6, 333),
    Level(r"\d{6}$", "县级", 6, 2846),
)


@dataclass
class RawRow:
    code: str
    name: str
    level_index: int


@dataclass
class BuildResult:
    rows: list[RawRow] = field(default_factory=list)
    #: 代码 → 父代码（None = 直接挂在国家下）
    parent_of: dict[str, str | None] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in self.rows:
            out[LEVELS[row.level_index].label] = out.get(LEVELS[row.level_index].label, 0) + 1
        return out


# --------------------------------------------------------------------------- #
#  解析
# --------------------------------------------------------------------------- #

_TAG_RE = re.compile(r"<[^>]+>")
_ROW_RE = re.compile(r"(?s)<tr[^>]*>.*?</tr>")
_CELL_RE = re.compile(r"(?s)<td[^>]*>(.*?)</td>")
_CODE_RE = re.compile(r"^\d{6,12}$")


def _cell_text(html: str) -> str:
    """单元格文本：去标签、解 `&nbsp;`、折叠空白。

    ⚠️ 坑 1：右侧列用 `&nbsp;` + 空格做**层级缩进**，不清洗会把缩进带进名字。
    实测 `450102 &nbsp;&nbsp; 兴宁区` → 清洗后 `兴宁区`。
    """
    text = _TAG_RE.sub("", html)
    text = text.replace("&nbsp;", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_mca_html(path: Path) -> BuildResult:
    """把民政部那份 HTML 解析成 (code, name, level) 列表。

    只认 ``<td>代码</td><td>名称</td>`` 形状的行，其余（标题行、表头、
    空行）一律跳过 —— 实测 3228 行里 3213 行是数据，15 行是标题/表头/空。
    """
    result = BuildResult()
    text = path.read_text(encoding="utf-8", errors="replace")

    for row_html in _ROW_RE.findall(text):
        cells = _CELL_RE.findall(row_html)
        if len(cells) < 3:
            continue
        code = _TAG_RE.sub("", cells[1]).strip()
        name = _cell_text(cells[2])
        if not _CODE_RE.match(code) or not name:
            continue

        # 坑 2：`济源市*` —— `*` 是民政部的"省直辖"标注，不是名字的一部分
        name = name.rstrip("*").strip()
        if not name:
            continue

        level_index = _level_index(code)
        if level_index is None:
            result.problems.append(f"无法判定层级的代码：{code} {name}")
            continue
        result.rows.append(RawRow(code=code, name=name, level_index=level_index))

    return result


def _level_index(code: str) -> int | None:
    """代码 → 层级下标。**浅层优先**匹配（`xx0000` 先于 `xxxx00`）。"""
    for index in range(len(LEVELS)):
        level = LEVELS[index]
        if len(code) == level.code_len and re.search(level.suffix_pattern, code):
            return index
    return None


# --------------------------------------------------------------------------- #
#  建树（坑 3：不能纯靠码前缀推父）
# --------------------------------------------------------------------------- #


def derive_parents(rows: list[RawRow]) -> dict[str, str | None]:
    """给每行定父代码。

    ## 为什么不能只用"前 4 位 + 00"

    `110101 东城区` 的"前 4 位 `110100`"**在数据里不存在** —— 直辖市是
    `省级 → 县级`，中间层留空（`models.py` 里写明"不做北京市-北京市-东城区的取巧"）。
    这类共 **118 个**（4 个直辖市 + 省直辖县级市，如济源、仙桃、神农架）。

    ## 做法：逐级上溯

    对一个代码，候选父是"比它短的、以它的前缀开头的、且**确实存在**的代码"，
    从最长的候选开始找，第一个存在的就是父。
    """
    exists = {row.code: row for row in rows}
    parent_of: dict[str, str | None] = {}

    for row in rows:
        parent: str | None = None
        # 候选：把自己的末尾逐段削掉（6 位 → 4 位+00 → 2 位+0000）
        code = row.code
        for cut in range(len(code) - 2, 1, -2):
            candidate = code[:cut] + "0" * (len(code) - cut)
            if candidate != code and candidate in exists:
                parent = candidate
                break
        parent_of[row.code] = parent

    return parent_of


# --------------------------------------------------------------------------- #
#  自检
# --------------------------------------------------------------------------- #


def self_check(result: BuildResult) -> list[str]:
    """**不能省的一步。** 父子关系错了不会报错，只会让补出来的省籍静默错。

    返回问题列表（空 = 通过）。
    """
    problems: list[str] = list(result.problems)
    rows = result.rows
    by_code = {row.code: row for row in rows}

    # 1. 层级数量与国标公布的数字比对（偏差要能解释）
    for index, level in enumerate(LEVELS):
        if level.expect_about is None:
            continue
        actual = sum(1 for row in rows if row.level_index == index)
        drift = abs(actual - level.expect_about) / level.expect_about
        mark = "" if drift <= 0.05 else "  ⚠️ 偏差 >5%，请确认数据年份"
        print(f"    {level.label}: {actual} 个（国标约 {level.expect_about}）{mark}")

    # 2. 孤儿：非省级节点必须有父，且父的层级必须更浅
    orphans = [
        row
        for row in rows
        if row.level_index > 0 and result.parent_of.get(row.code) is None
    ]
    if orphans:
        problems.append(f"孤儿节点 {len(orphans)} 个，例如 {orphans[:5]}")

    for row in rows:
        parent_code = result.parent_of.get(row.code)
        if parent_code is None:
            continue
        parent = by_code.get(parent_code)
        if parent is None:
            problems.append(f"{row.code} {row.name} 的父 {parent_code} 不在数据里")
            continue
        if parent.level_index >= row.level_index:
            problems.append(
                f"{row.code} {row.name}（{LEVELS[row.level_index].label}）"
                f"的父 {parent_code}（{LEVELS[parent.level_index].label}）层级不更浅"
            )

    # 3. 代码长度与层级一致
    for row in rows:
        expected = LEVELS[row.level_index].code_len
        if len(row.code) != expected:
            problems.append(f"{row.code} 的长度 {len(row.code)} 与 {LEVELS[row.level_index].label}（{expected}）不符")

    # 4. 同名前缀的代码是否真的同属一个父（抽查省级一致性）
    prefix_mismatch = 0
    for row in rows:
        if row.level_index < 2:
            continue
        parent_code = result.parent_of.get(row.code)
        if parent_code and not row.code.startswith(parent_code[:2]):
            prefix_mismatch += 1
    if prefix_mismatch:
        problems.append(f"{prefix_mismatch} 个节点的父与自己的省级前缀不一致（数据可能跨省错挂）")

    # 5. 名字里还有残留的缩进/实体
    bad_names = [row for row in rows if "&" in row.name or row.name != row.name.strip()]
    if bad_names:
        problems.append(f"{len(bad_names)} 个名字没清洗干净，例如 {bad_names[:3]}")

    return problems


def vocab_preview(rows: list[RawRow]) -> None:
    """打印词表会产出哪些戳面写法 —— 这是"数据灌对了没有"的最直观检查。"""
    from postmarktrip.recognition.vocab import place_short_name  # noqa: PLC0415

    print()
    print("  词表抽查（标准名 → 戳面短名）：")
    wanted = ["北京市", "广西壮族自治区", "内蒙古自治区", "南平市", "玉山县",
              "青龙满族自治县", "围场满族蒙古族自治县", "莫力达瓦达斡尔族自治旗",
              "鄂温克族自治旗", "龙胜各族自治县", "赵县", "横州市", "东城区"]
    index = {row.name: row for row in rows}
    for name in wanted:
        row = index.get(name)
        if row is None:
            print(f"    {name:26} **不在数据里**")
            continue
        print(f"    {name:26} -> {place_short_name(name)!r}   [代码 {row.code}]")


# --------------------------------------------------------------------------- #
#  写库
# --------------------------------------------------------------------------- #


def prune_seed(db: object) -> int:
    """删掉**不属于本次数据**的区划节点（演示/手工种子）。

    ## 为什么需要它

    开发期为了演示"绿勾规则"手工灌过一棵小树（`福建省`/`南平市`/…），
    它们的 `admin_code` 与真数据**撞车但 id 不同**。导入真数据之后会同时存在
    两份「福建省」，而词表按 `name` 建索引，两份会互相覆盖 —— 症状是
    "补出来的省籍时对时错"，很难查。

    ## 判据（保守）

    只删**同时满足**这些的节点：`admin_code` 为空 **或** 长度不是 6 位，
    **且** 没有任何子节点引用它。这样：
      * 真数据的节点（6 位 `admin_code`）一个都不会被删
      * 国家根节点（`admin_code` 为空但**有子节点**）也不会被删
      * 只有手工种子那种"孤立的、没有真代码的"节点会被清掉
    """
    from sqlalchemy import func, select  # noqa: PLC0415

    from postmarktrip.db.models import Place  # noqa: PLC0415

    child_count = (
        select(func.count())
        .select_from(Place)
        .where(Place.parent_id == Place.id)
        .correlate(Place)
        .scalar_subquery()
    )
    candidates = list(
        db.scalars(  # type: ignore[attr-defined]
            select(Place).where(
                (Place.admin_code.is_(None) | (func.length(Place.admin_code) != 6)),
                child_count == 0,
                Place.level != 0,  # 根节点单独保护
            )
        )
    )
    for node in candidates:
        db.delete(node)  # type: ignore[attr-defined]
    return len(candidates)


def import_into_db(result: BuildResult, *, dry_run: bool, prune: bool = False) -> int:
    from sqlalchemy import select  # noqa: PLC0415

    from postmarktrip.api.routes_dict import _compute_path  # noqa: PLC0415
    from postmarktrip.db.models import Place  # noqa: PLC0415
    from postmarktrip.db.session import session_scope  # noqa: PLC0415

    created = 0
    updated = 0
    with session_scope() as db:
        # 幂等：按 admin_code 找已有的（`place` 表没有唯一约束，只能自己判）
        existing = {
            place.admin_code: place
            for place in db.scalars(select(Place).where(Place.country_code == "CN"))
            if place.admin_code
        }

        # 根节点：数据里没有，补一个。词表的层级链从它开始。
        #
        # ⚠️ 判据必须**三道都给**（`level == 0` + 无 admin_code + 无父）。
        # 我第一版只按 `parent_id IS NULL` 找，结果把演示种子里的「福建省」
        # （level=1、有 admin_code）当成了根 —— 干跑时打印
        # 「已有根节点：福建省」才发现。
        root = db.scalar(
            select(Place)
            .where(
                Place.parent_id.is_(None),
                Place.country_code == "CN",
                Place.level == 0,
                Place.admin_code.is_(None),
            )
            .limit(1)
        )
        if root is None:
            root = Place(
                name=COUNTRY_NAME,
                name_en=None,
                level=0,
                level_label="国家",
                country_code="CN",
                admin_code=None,
            )
            if not dry_run:
                db.add(root)
                db.flush()
                root.path = _compute_path(db, root, None)
                db.flush()
            created += 1
            print(f"  建根节点：{COUNTRY_NAME}")
        else:
            print(f"  已有根节点：{root.name}（{root.id}）")

        # 按层级从浅到深插入：父必须先存在（要用它的 path）
        by_level: dict[int, list[RawRow]] = {}
        for row in result.rows:
            by_level.setdefault(row.level_index, []).append(row)

        code_to_place: dict[str, Place] = {}
        if root is not None and not dry_run:
            code_to_place[COUNTRY_CODE] = root

        for level_index in sorted(by_level):
            level = LEVELS[level_index]
            batch = by_level[level_index]
            print(f"  处理 {level.label}：{len(batch)} 个")
            for row in batch:
                existing_place = existing.get(row.code)
                parent = None
                parent_code = result.parent_of.get(row.code)
                if parent_code is None:
                    parent = root
                else:
                    parent = code_to_place.get(parent_code)

                if existing_place is not None:
                    # 名字可能被民政部改过（改市、改区），以本次数据为准
                    if existing_place.name != row.name:
                        if not dry_run:
                            existing_place.name = row.name
                        updated += 1
                    code_to_place[row.code] = existing_place
                    continue

                if dry_run:
                    created += 1
                    continue

                node = Place(
                    name=row.name,
                    level=level_index + 1,  # 0 留给国家
                    level_label=level.label,
                    country_code="CN",
                    admin_code=row.code,
                    parent_id=parent.id if parent is not None else None,
                )
                db.add(node)
                db.flush()
                node.path = _compute_path(db, node, parent)
                db.flush()
                code_to_place[row.code] = node
                created += 1

            if not dry_run:
                db.flush()

        if prune and not dry_run:
            removed = prune_seed(db)
            print(f"  清理演示/手工种子节点：{removed} 个")

    print(f"  新增 {created} 个节点，更新名字 {updated} 个")
    return created


# --------------------------------------------------------------------------- #
#  入口
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把民政部行政区划代码表灌进 place 表")
    parser.add_argument("html", type=Path, help="民政部那份 HTML（另存为的 Excel-HTML）")
    parser.add_argument("--dry-run", action="store_true", help="只出报告，不写库")
    parser.add_argument("--skip-vocab", action="store_true", help="跳过词表抽查")
    parser.add_argument(
        "--prune",
        action="store_true",
        help="写库后清掉孤立的、没有 6 位行政代码的节点（开发期手工种子）",
    )
    args = parser.parse_args(argv)

    if not args.html.is_file():
        print(f"找不到文件：{args.html}", file=sys.stderr)
        return 2

    print(f"=== 解析 {args.html.name} ===")
    result = parse_mca_html(args.html)
    if not result.rows:
        print("没解析出任何数据行 —— 文件形状和预期不符（应当是 Excel 导出的 HTML 表格）")
        return 1
    print(f"  数据行 {len(result.rows)}")

    print()
    print("=== 定父（逐级上溯）===")
    result.parent_of = derive_parents(result.rows)

    print()
    print("=== 自检 ===")
    problems = self_check(result)
    if problems:
        print(f"  **发现 {len(problems)} 个问题**")
        for problem in problems[:20]:
            print(f"    - {problem}")
    else:
        print("  通过（孤儿 0、层级数量吻合、代码长度一致）")

    if not args.skip_vocab:
        vocab_preview(result.rows)

    print()
    print("=== " + ("干跑（不写库）" if args.dry_run else "写库") + " ===")
    import_into_db(result, dry_run=args.dry_run, prune=args.prune)

    if problems and not args.dry_run:
        print()
        print("⚠️ 自检有问题但仍然写库了 —— 请复核上面的问题清单")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
