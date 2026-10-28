"""官方发行页解析（M3C-3）。

只做 **HTML 表格 + 少量文本启发式**：
* 中国集邮/邮政的“图序 / 图名 / 面值”表格；
* “邮票志号 / 发行日期 / 齿孔度数 / 印刷版别 / 设计者 / 印刷厂…”键值表；
* 没有的字段留空（不猜）。

解析结果**不直接写库**：先放进 `details.last_parse`，由复核 UI 选择图序后再应用。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any

PARSE_VERSION = "cn-official-table-v1"

#: 键值表标签 → details 字段名（`catalog_ref` / `issue_date` 单独处理）
_DETAIL_LABELS = {
    "邮票规格": "size_mm",
    "齿孔度数": "perforation",
    "整版枚数": "sheet_count",
    "整版规格": "sheet_size_mm",
    "印刷版别": "printing",
    "防伪方式": "security",
    "设计者": "designer",
    "责任编辑": "editor",
    "印刷厂": "printer",
}

_DATE_RES = (
    re.compile(r"(?P<y>\d{4})[-./](?P<m>\d{1,2})[-./](?P<d>\d{1,2})"),
    re.compile(r"(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日"),
)


@dataclass
class ParsedDesign:
    design_no: str | None
    design_name: str | None
    face_value: Decimal | None
    currency: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "design_no": self.design_no,
            "design_name": self.design_name,
            "face_value": format(self.face_value, "f") if self.face_value is not None else None,
            "currency": self.currency,
        }


@dataclass
class ParsedCatalog:
    issue_name: str | None
    catalog_ref: str | None
    issue_date: date | None
    details: dict[str, str] = field(default_factory=dict)
    designs: list[ParsedDesign] = field(default_factory=list)
    field_sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    source_url: str = ""
    parse_version: str = PARSE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "parse_version": self.parse_version,
            "source_url": self.source_url,
            "issue_name": self.issue_name,
            "catalog_ref": self.catalog_ref,
            "issue_date": self.issue_date.isoformat() if self.issue_date else None,
            "details": self.details,
            "designs": [d.to_dict() for d in self.designs],
            "field_sources": self.field_sources,
        }


class _TableReader(HTMLParser):
    """抽 `<table>` 的单元格文本 + 页面标题，不引 bs4。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self.title = ""
        self.h1 = ""
        self._table_depth = 0
        self._current_table: list[list[str]] = []
        self._current_row: list[str] | None = None
        self._cell_parts: list[str] | None = None
        self._in_title = False
        self._in_h1 = False
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in ("script", "style"):
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        elif tag == "h1":
            self._in_h1 = True
        elif tag == "table":
            if self._table_depth == 0:
                self._current_table = []
            self._table_depth += 1
        elif tag == "tr" and self._table_depth == 1:
            self._flush_cell()
            self._flush_row()
            self._current_row = []
        elif tag in ("td", "th") and self._table_depth == 1:
            self._flush_cell()
            self._cell_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("script", "style"):
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False
        elif tag in ("td", "th") and self._table_depth == 1:
            self._flush_cell()
        elif tag == "tr" and self._table_depth == 1:
            self._flush_cell()
            self._flush_row()
        elif tag == "table":
            if self._table_depth == 1:
                self._flush_cell()
                self._flush_row()
                self._flush_table()
            self._table_depth = max(0, self._table_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_title:
            self.title += data
        if self._in_h1:
            self.h1 += data
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    # 目标页的 <td>/<th>/<tr> 经常不闭合；必须在下一个开始标签处手动收口。
    def _flush_cell(self) -> None:
        if self._current_row is not None and self._cell_parts is not None:
            self._current_row.append(_clean("".join(self._cell_parts)))
        self._cell_parts = None

    def _flush_row(self) -> None:
        if self._current_row is not None:
            self._current_table.append(self._current_row)
        self._current_row = None

    def _flush_table(self) -> None:
        if self._current_table:
            self.tables.append(self._current_table)
        self._current_table = []


def _clean(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").split())


def _strip_label(text: str) -> str:
    # 官方页的“设 计 者:”“印 刷 厂:”在字符间插了空格；标签归一化时去掉。
    return re.sub(r"\s+", "", text).rstrip(":：").strip()


def _title_without_site(title: str) -> str | None:
    value = _clean(title)
    if not value:
        return None
    for sep in (" - ", "_", "|"):
        if sep in value:
            value = value.split(sep, 1)[0].strip()
    return value or None


def parse_issue_date(text: str | None) -> date | None:
    if not text:
        return None
    for pattern in _DATE_RES:
        match = pattern.search(text)
        if not match:
            continue
        try:
            return date(int(match.group("y")), int(match.group("m")), int(match.group("d")))
        except ValueError:
            continue
    return None


def parse_face_value(text: str | None) -> tuple[Decimal | None, str | None]:
    """'1.20 元' → (Decimal('1.20'), 'CNY')；'40 分' → (Decimal('0.40'), 'CNY')。"""
    if not text:
        return None, None
    raw = _clean(text).replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*(元|圆|分|角)?", raw)
    if not match:
        return None, None
    try:
        value = Decimal(match.group(1))
    except InvalidOperation:
        return None, None
    unit = match.group(2) or "元"
    if unit == "分":
        return value / 100, "CNY"
    if unit == "角":
        return value / 10, "CNY"
    if "美元" in raw or "USD" in raw.upper():
        return value, "USD"
    if "日元" in raw or "JPY" in raw.upper():
        return value, "JPY"
    return value, "CNY"


def normalize_design_no(value: str | None) -> str:
    """只用于匹配：全角→半角、去空白、大写；不改变入库的原样值。"""
    if not value:
        return ""
    return unicodedata.normalize("NFKC", value).replace(" ", "").strip().upper()


def parse_catalog_html(
    html: str, *, source_url: str, field_source: str = "issue_url"
) -> ParsedCatalog:
    reader = _TableReader()
    reader.feed(html)

    issue_name = _title_without_site(reader.h1) or _title_without_site(reader.title)
    details: dict[str, str] = {}
    catalog_ref: str | None = None
    issue_date: date | None = None
    designs: list[ParsedDesign] = []

    for table in reader.tables:
        # 1) “图序 / 图名 / 面值”表
        header_index = None
        for index, row in enumerate(table):
            compact = [re.sub(r"\s+", "", cell) for cell in row]
            if any("图序" in cell for cell in compact) and any("图名" in cell for cell in compact):
                header_index = index
                break
        if header_index is not None:
            for row in table[header_index + 1 :]:
                if len(row) < 3 or not row[0]:
                    continue
                face, currency = parse_face_value(row[2])
                designs.append(
                    ParsedDesign(
                        design_no=row[0] or None,
                        design_name=row[1] or None,
                        face_value=face,
                        currency=currency,
                    )
                )
            continue

        # 2) “标签: 值”表
        for row in table:
            if len(row) != 2:
                continue
            label = _strip_label(row[0])
            value = row[1].strip()
            if not label or not value:
                continue
            if label in ("邮票志号", "志号", "目录号"):
                catalog_ref = value
            elif label in ("发行日期", "发行时间"):
                issue_date = parse_issue_date(value)
            elif label in _DETAIL_LABELS:
                details[_DETAIL_LABELS[label]] = value

    sources: dict[str, dict[str, Any]] = {
        "issue_name": {"source": field_source, "confidence": 0.85},
        "catalog_ref": {"source": field_source, "confidence": 0.95},
        "issue_date": {"source": field_source, "confidence": 0.95},
    }
    for key in details:
        sources[key] = {"source": field_source, "confidence": 0.9}
    for key in ("design_no", "design_name", "face_value"):
        sources[key] = {"source": field_source, "confidence": 0.9}

    return ParsedCatalog(
        issue_name=issue_name,
        catalog_ref=catalog_ref,
        issue_date=issue_date,
        details=details,
        designs=designs,
        field_sources={k: v for k, v in sources.items() if k in (
            "issue_name", "catalog_ref", "issue_date", *details.keys()
        ) or k in ("design_no", "design_name", "face_value")},
        source_url=source_url,
    )


def pick_design(
    parsed: ParsedCatalog,
    requested_design_no: str | None,
    current_design_no: str | None,
) -> ParsedDesign | None:
    """从一页多枚里选一枚；只有一枚时直接选它。"""
    if not parsed.designs:
        return None
    if len(parsed.designs) == 1:
        return parsed.designs[0]
    wanted = normalize_design_no(requested_design_no or current_design_no)
    if wanted:
        for design in parsed.designs:
            if normalize_design_no(design.design_no) == wanted:
                return design
    return None
