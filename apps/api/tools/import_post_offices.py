#!/usr/bin/env python3
"""把中国邮政的**服务网点列表**（CSV/XLSX）导入 `post_office`。

## 数据源：中国邮政官网那份（用户 2026-10-11 提供）

`inbox/邮政网点列表.csv` 与 `.xlsx` —— **两份完全一致**（实测 54620 行、
54609 个不同四元组，差异只是 CSV 里 3 行末尾多个全角空格 `\\u3000`）。

列：`省, 市, 县, 服务网点名称, 邮编, 地址, 是否办理金融业务, 电话, 营业时间`

### 为什么用这份，不用国家邮政局那份

| | 这份（中国邮政） | 国家邮政局平台 |
| --- | --- | --- |
| 行数 | **54620** | 54613 |
| 名称 | **`三清山邮政所`（干净，无前缀）** | 15.3% 带 `中国邮政集团有限公司…` 前缀 |
| 字段 | 9 列（含邮编/地址/金融/电话/营业时间） | 5 列 |
| 状态 | 有 388 条（`（暂停）`384、`（撤销）`4） | 无 |

**名字干净这一条最值钱**：它让"剥掉公司前缀与省市县前缀"这一整类规则
**完全不需要** —— 而我在平台那份上面为此错过两次（用短名做子串匹配 → 750 条
后缀残留；只在有公司前缀时才剥 → 17252 条仍带地名）。**规则能不写就不写。**

**没有前缀就一定没有地名前缀吗？** 不完全是 —— 实测仍有极少数名字带地名
（`合肥市王大郢邮政所`、`枞阳县城关第三邮政所`）。但这类**不做处理**：
它们是真实的服务网点名称，强行切反而错（`芜湖路` 会被 `芜湖` 吃掉）。
是否要剥留给将来的模糊匹配，而不是在这里猜。

## 字段映射

| CSV 列 | 落到哪 | 说明 |
| --- | --- | --- |
| 省/市/县 | → `place_id` | 按 `县 → 市 → 省` 顺序**精确名**匹配；匹配不上留空并报出 |
| 服务网点名称 | → `name` | **原样存**，不做任何规范化 |
| （同一行） | → `aliases` | `[]`（保留给人工加别名） |
| 邮编 | → `postal_code` | |
| （名称里的状态标记） | → `note` | `（暂停）`/`（撤销）` 是**状态**，不进 `name` |
| 地址 | → 拼进 `note` | 目录里没有"地址"列 |
| 是否办理金融业务 / 电话 / 营业时间 | → 拼进 `note` | 同上；这些是**运营信息**，不是识别要用的 |

⚠️ **`note` 是"目录原始信息"的落点，不是给人读的结论。** 导入时写进去的是
客观事实（地址、电话、状态）；**OCR 永不自动写它**。

## 用法

```bash
cd apps/api
python tools/import_post_offices.py ~/myCode/postmarkTrip/inbox/邮政网点列表.csv --dry-run
python tools/import_post_offices.py ~/myCode/postmarkTrip/inbox/邮政网点列表.csv
```
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

#: 与 `tools/` 里既有工具同一个约定：把 apps/api/src 挂上。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

#: 名称里可能带**状态标记**。实测只有这三种，但写成通用括号匹配更稳。
STATUS_RE: Final = re.compile(r"[（(](暂停|撤销|停业|装修|筹建)[）)]\s*$")

#: 期望的列名（缺失就报错，不猜）
REQUIRED_COLUMNS: Final = ("省", "市", "县", "服务网点名称")

#: source 字段的值：标明来源与抓取日，便于将来回答"这批数据哪来的"
SOURCE: Final = "china-post-网点列表 (用户导出 2026-10-11)"


@dataclass
class OfficeRow:
    """CSV 的一行（已清洗）。"""

    province: str
    city: str
    county: str
    name: str
    postal_code: str
    address: str
    finance: str
    phone: str
    hours: str
    #: 原样的名称（含状态标记），用于写进 note
    raw_name: str
    #: 状态标记（`暂停`/`撤销`/…），空串 = 无
    status: str

    @property
    def note(self) -> str:
        """目录原始信息拼成备注。**客观事实，不做判断。**"""
        parts: list[str] = []
        if self.status:
            parts.append(f"目录状态：{self.status}")
        if self.address:
            parts.append(f"地址：{self.address}")
        if self.postal_code:
            parts.append(f"邮编：{self.postal_code}")
        if self.phone:
            parts.append(f"电话：{self.phone}")
        if self.finance:
            parts.append(f"办理金融业务：{self.finance}")
        if self.hours:
            parts.append(f"营业时间：{self.hours}")
        return "；".join(parts)


@dataclass
class Report:
    rows: list[OfficeRow] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _clean(value: object) -> str:
    """去首尾空白，**含全角空格**（实测 CSV 里有 3 行带 `\\u3000`）。"""
    return str(value or "").strip().strip("\u3000").strip()


def parse_csv(path: Path) -> Report:
    """读 CSV。表头必须齐全，否则直接报错（不猜列）。"""
    report = Report()
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        missing = [c for c in REQUIRED_COLUMNS if c not in header]
        if missing:
            report.problems.append(f"缺少必需列：{missing}（实际列：{header}）")
            return report
        for lineno, raw in enumerate(reader, start=2):
            name_raw = _clean(raw.get("服务网点名称"))
            if not name_raw:
                report.problems.append(f"第 {lineno} 行没有网点名称")
                continue
            m = STATUS_RE.search(name_raw)
            status = m.group(1) if m else ""
            name = STATUS_RE.sub("", name_raw).strip()
            report.rows.append(
                OfficeRow(
                    province=_clean(raw.get("省")),
                    city=_clean(raw.get("市")),
                    county=_clean(raw.get("县")),
                    name=name,
                    postal_code=_clean(raw.get("邮编")),
                    address=_clean(raw.get("地址")),
                    finance=_clean(raw.get("是否办理金融业务")),
                    phone=_clean(raw.get("电话")),
                    hours=_clean(raw.get("营业时间")),
                    raw_name=name_raw,
                    status=status,
                )
            )
    return report


# --------------------------------------------------------------------------- #
#  自检与预览
# --------------------------------------------------------------------------- #


def describe(report: Report) -> None:
    from collections import Counter

    rows = report.rows
    print(f"  解析出 {len(rows)} 行")

    print()
    print("  --- 省分布（前 12）---")
    for name, n in Counter(r.province for r in rows).most_common(12):
        print(f"    {name:14} {n}")

    print()
    print("  --- 状态标记 ---")
    st = Counter(r.status for r in rows if r.status)
    print(f"    带状态 {sum(st.values())} 条：{dict(st)}")

    print()
    print("  --- 名称形态抽样 ---")
    for r in rows[:3]:
        print(f"    [{r.province} {r.city} {r.county}]")
        print(f"       name={r.name!r}  邮编={r.postal_code!r}")
        print(f"       note={r.note[:90]!r}…")

    print()
    print("  --- 名称里仍带地名的（不处理，只报数）---")
    n_place = sum(
        1
        for r in rows
        if any(r.name.startswith(p) for p in (r.province, r.city, r.county) if p)
    )
    print(f"    {n_place} 条（{n_place/len(rows)*100:.1f}%）—— 原样保留，不切")

    print()
    print("  --- 名称长度分布 ---")
    lens = Counter(len(r.name) for r in rows)
    for n, c in sorted(lens.items())[:14]:
        print(f"    {n} 字: {c}")
    print(f"    （最长 {max(lens)} 字）")

    print()
    print("  --- 空名 / 重名 ---")
    print(f"    空名 {sum(1 for r in rows if not r.name)} 条")
    dup = Counter((r.province, r.city, r.county, r.name) for r in rows)
    dups = {k: v for k, v in dup.items() if v > 1}
    print(f"    同县同名重复 {len(dups)} 组，涉及 {sum(dups.values())} 条")
    for k, v in list(dups.items())[:5]:
        print(f"      {k} ×{v}")


# --------------------------------------------------------------------------- #
#  写库
# --------------------------------------------------------------------------- #


def import_into_db(report: Report, *, dry_run: bool) -> None:
    from sqlalchemy import select

    from postmarktrip.db.models import Place, PostOffice
    from postmarktrip.db.session import session_scope

    with session_scope() as db:
        #: 省/市/县名 → place id。**按精确名匹配**（同一份国标命名）。
        #
        # ⚠️ 不要用 `.tuples()` —— SQLAlchemy 2.1 起已废弃，而 `_dev.sh` 用
        # `-W error::DeprecationWarning` 跑，真导入会直接抛错（实测踩过）。
        place_by_name: dict[str, object] = {}
        for pid, name in db.execute(select(Place.id, Place.name)).all():
            place_by_name.setdefault(name, pid)
        print(f"  place 表里有 {len(place_by_name)} 个不同名字可供匹配")

        #: 幂等：`(名称, 所属 place)` 视为同一条
        existing = {(row.name, row.place_id) for row in db.scalars(select(PostOffice)).all()}
        print(f"  已有邮局 {len(existing)} 条")

        created = 0
        skipped = 0
        unmatched: dict[str, int] = {}
        for r in report.rows:
            place_id = (
                place_by_name.get(r.county)
                or place_by_name.get(r.city)
                or place_by_name.get(r.province)
            )
            if place_id is None:
                key = f"{r.province}/{r.city}/{r.county}"
                unmatched[key] = unmatched.get(key, 0) + 1

            if (r.name, place_id) in existing:
                skipped += 1
                continue
            if dry_run:
                created += 1
                continue

            #: ⚠️ `is_theme` / `theme_name` **不在这里猜** —— 目录不含"是不是主题
            #: 邮局"的信息。`三清山主题邮局` 这种戳上出现的名字在目录里查不到
            #: （只有 `三清山邮政所`），属"戳名"范畴，住 `postmark.inscription`。
            db.add(
                PostOffice(
                    name=r.name,
                    place_id=place_id,
                    postal_code=r.postal_code or None,
                    is_theme=False,
                    theme_name=None,
                    aliases=json.loads("[]"),
                    note=r.note or None,
                    source=SOURCE,
                )
            )
            created += 1

        if not dry_run:
            db.flush()

    print(f"  新增 {created} 条，已存在跳过 {skipped} 条")
    if unmatched:
        print(f"  ⚠️ place 匹配不上的 (省/市/县) 组合 {len(unmatched)} 种：")
        for k, v in list(unmatched.items())[:12]:
            print(f"      {k}  × {v}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入中国邮政服务网点列表")
    parser.add_argument("csv", type=Path, help="网点列表 CSV（或 .xlsx 需先转 CSV）")
    parser.add_argument("--dry-run", action="store_true", help="只出报告，不写库")
    args = parser.parse_args(argv)

    if not args.csv.is_file():
        print(f"找不到文件：{args.csv}", file=sys.stderr)
        return 2
    if args.csv.suffix.lower() != ".csv":
        print(
            f"只吃 CSV。{args.csv.suffix} 请先转成 CSV（xlsx 与 csv 实测内容一致，"
            f"所以直接用同名 .csv 即可）",
            file=sys.stderr,
        )
        return 2

    print(f"=== 解析 {args.csv.name} ===")
    report = parse_csv(args.csv)
    if report.problems:
        print(f"  **{len(report.problems)} 个解析问题**")
        for p in report.problems[:10]:
            print(f"    {p}")
    if not report.rows:
        print("没有解析出任何数据行")
        return 1

    describe(report)

    print()
    print("=== " + ("干跑（不写库）" if args.dry_run else "写库") + " ===")
    import_into_db(report, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
