#!/usr/bin/env python3
"""从 Natural Earth 的省级边界生成 `apps/web/src/assets/china-provinces.svg`。

## 为什么要有这个脚本

M3d 的地图是**形态 B：静态省份图**（`docs/spec/m3/04-地图.md` §三）：
一张按 `place.admin_code` 前两位着色的 SVG，不是 Leaflet / 瓦片 / 打点地图。
边界数据**不手画**，来自 Natural Earth（public domain）：

* `ne_10m_admin_1_states_provinces` —— 中国的 31 个省级行政区（省 / 自治区 / 直辖市）；
* `ne_10m_admin_0_countries` —— 台湾 / 香港 / 澳门（Natural Earth 把它们放在国家层，
  不放在中国的 admin_1 里；我们原样取用，不手改边界）。

Natural Earth 的 `iso_3166_2` 是 `CN-FJ` 这种编码，不是 `35`；`ISO_TO_CODE`
就是那张 `CN-FJ → 35` 的映射表（34 个省级，GB/T 2260 前两位）。

## 复现命令

```bash
# 1) 下载（约 15 MB + 9 MB；这是 Natural Earth 的官方 CDN）
curl -LO https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_1_states_provinces.zip
curl -LO https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_countries.zip
unzip -d ne10 ne_10m_admin_1_states_provinces.zip
unzip -d ne0  ne_10m_admin_0_countries.zip

# 2) 生成（需要 pyshp：pip install pyshp）
python tools/make-china-svg.py \
    --admin1 ne10/ne_10m_admin_1_states_provinces.shp \
    --admin0 ne0/ne_10m_admin_0_countries.shp \
    --out apps/web/src/assets/china-provinces.svg

# 3) 可选：生成一张 PNG 用于肉眼检查
python tools/make-china-svg.py ... --preview /tmp/china-provinces.png
```

## 投影与简化

中国经度跨度 62°、纬度 35°，等经纬度投影会让北部明显拉长。这里用
**Albers 等积圆锥投影**（标准纬线 25°N / 47°N，中央经线 105°E），
与常见的中国全图制图习惯一致。简化用 Douglas–Peucker，容差按输出画布
（宽 1000 单位）给，默认 0.45 单位 —— 约 0.3 像素，肉眼看不出来。

产物只保留 `data-code`（`admin_code` 前两位）与 `data-name`（中文名）；
颜色、hover、点击全部由前端 `MapView` 决定，SVG 本身**不含任何数据**。
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import shapefile  # pyshp

# --------------------------------------------------------------------------- #
#  省级编码（GB/T 2260 前两位）与中文名
# --------------------------------------------------------------------------- #

#: Natural Earth `iso_3166_2` → 两位省级代码。
ISO_TO_CODE: dict[str, str] = {
    "CN-AH": "34",  # 安徽
    "CN-BJ": "11",  # 北京
    "CN-CQ": "50",  # 重庆
    "CN-FJ": "35",  # 福建
    "CN-GD": "44",  # 广东
    "CN-GS": "62",  # 甘肃
    "CN-GX": "45",  # 广西
    "CN-GZ": "52",  # 贵州
    "CN-HA": "41",  # 河南
    "CN-HB": "42",  # 湖北
    "CN-HE": "13",  # 河北
    "CN-HI": "46",  # 海南
    "CN-HL": "23",  # 黑龙江
    "CN-HN": "43",  # 湖南
    "CN-JL": "22",  # 吉林
    "CN-JS": "32",  # 江苏
    "CN-JX": "36",  # 江西
    "CN-LN": "21",  # 辽宁
    "CN-NM": "15",  # 内蒙古
    "CN-NX": "64",  # 宁夏
    "CN-QH": "63",  # 青海
    "CN-SC": "51",  # 四川
    "CN-SD": "37",  # 山东
    "CN-SH": "31",  # 上海
    "CN-SN": "61",  # 陕西
    "CN-SX": "14",  # 山西
    "CN-TJ": "12",  # 天津
    "CN-XJ": "65",  # 新疆
    "CN-XZ": "54",  # 西藏
    "CN-YN": "53",  # 云南
    "CN-ZJ": "33",  # 浙江
}

#: 从 admin_0 取的三地（Natural Earth 不把港澳台放进中国 admin_1）。
ADMIN0_CODES: dict[str, str] = {
    "TW": "71",
    "HK": "81",
    "MO": "82",
}

#: 两位代码 → 中文名（与 `place.name` 的写法对齐）。
CODE_TO_NAME: dict[str, str] = {
    "11": "北京市",
    "12": "天津市",
    "13": "河北省",
    "14": "山西省",
    "15": "内蒙古自治区",
    "21": "辽宁省",
    "22": "吉林省",
    "23": "黑龙江省",
    "31": "上海市",
    "32": "江苏省",
    "33": "浙江省",
    "34": "安徽省",
    "35": "福建省",
    "36": "江西省",
    "37": "山东省",
    "41": "河南省",
    "42": "湖北省",
    "43": "湖南省",
    "44": "广东省",
    "45": "广西壮族自治区",
    "46": "海南省",
    "50": "重庆市",
    "51": "四川省",
    "52": "贵州省",
    "53": "云南省",
    "54": "西藏自治区",
    "61": "陕西省",
    "62": "甘肃省",
    "63": "青海省",
    "64": "宁夏回族自治区",
    "65": "新疆维吾尔自治区",
    "71": "台湾省",
    "81": "香港特别行政区",
    "82": "澳门特别行政区",
}

#: 输出画布宽度（viewBox 单位）。高度按投影后的外接框算。
WIDTH = 1000.0
PADDING = 8.0


# --------------------------------------------------------------------------- #
#  投影
# --------------------------------------------------------------------------- #


def albers(lon: float, lat: float) -> tuple[float, float]:
    """Albers 等积圆锥投影（标准纬线 25°N / 47°N，中央经线 105°E）。

    公式见 Snyder, *Map Projections — A Working Manual* (USGS PP 1395) §14-9。
    用弧度计算；返回平面坐标（米级，但只取相对值）。
    """
    lon0 = math.radians(105.0)
    lat0 = math.radians(35.0)
    phi1 = math.radians(25.0)
    phi2 = math.radians(47.0)

    n = (math.sin(phi1) + math.sin(phi2)) / 2.0
    c = math.cos(phi1) ** 2 + 2.0 * n * math.sin(phi1)

    rho = math.sqrt(c - 2.0 * n * math.sin(math.radians(lat))) / n
    rho0 = math.sqrt(c - 2.0 * n * math.sin(lat0)) / n
    theta = n * (math.radians(lon) - lon0)

    return rho * math.sin(theta), rho0 - rho * math.cos(theta)


# --------------------------------------------------------------------------- #
#  简化
# --------------------------------------------------------------------------- #


def _perp_distance(
    point: tuple[float, float], start: tuple[float, float], end: tuple[float, float]
) -> float:
    """点到线段的距离。线段退化成点时退回点距。"""
    px, py = point
    sx, sy = start
    ex, ey = end
    dx, dy = ex - sx, ey - sy
    if dx == 0 and dy == 0:
        return math.hypot(px - sx, py - sy)
    t = ((px - sx) * dx + (py - sy) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (sx + t * dx), py - (sy + t * dy))


def douglas_peucker(
    points: list[tuple[float, float]], tolerance: float
) -> list[tuple[float, float]]:
    """Douglas–Peucker 折线简化（迭代实现，避免深递归）。

    输入是**闭合环**时调用方不要重复首点；返回值同样不重复。
    """
    if len(points) <= 2:
        return list(points)

    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack: list[tuple[int, int]] = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        max_distance = -1.0
        max_index = first
        for index in range(first + 1, last):
            distance = _perp_distance(points[index], points[first], points[last])
            if distance > max_distance:
                max_distance = distance
                max_index = index
        if max_distance > tolerance:
            keep[max_index] = True
            stack.append((first, max_index))
            stack.append((max_index, last))
    return [point for point, kept in zip(points, keep) if kept]


def ring_area(points: list[tuple[float, float]]) -> float:
    """闭合环的绝对面积（鞋带公式）。"""
    total = 0.0
    for index, (x1, y1) in enumerate(points):
        x2, y2 = points[(index + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


# --------------------------------------------------------------------------- #
#  读几何
# --------------------------------------------------------------------------- #


def _polygons(geometry: object) -> list[list[list[tuple[float, float]]]]:
    """GeoJSON 风格几何 → `[polygon][ring][point]`。"""
    if not isinstance(geometry, dict):
        return []
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates") or []
    if kind == "Polygon":
        return [coordinates]
    if kind == "MultiPolygon":
        return list(coordinates)
    return []


def collect_shapes(
    admin1_path: Path, admin0_path: Path
) -> dict[str, list[list[tuple[float, float]]]]:
    """返回 `code2 -> [ring, ...]`（经纬度）。一个省可能由多个环组成。"""
    shapes: dict[str, list[list[tuple[float, float]]]] = {}

    reader = shapefile.Reader(str(admin1_path), encoding="utf-8")
    for record in reader.iterShapeRecords():
        data = record.record.as_dict()
        if data.get("adm0_a3") != "CHN":
            continue
        code = ISO_TO_CODE.get(str(data.get("iso_3166_2") or ""))
        if code is None:
            # CN-X01~（西沙群岛）没有对应 `place` 节点，也不在 34 个省级里。
            continue
        for polygon in _polygons(record.shape.__geo_interface__):
            shapes.setdefault(code, []).extend(polygon)

    countries = shapefile.Reader(str(admin0_path), encoding="utf-8")
    for record in countries.iterShapeRecords():
        data = record.record.as_dict()
        iso = str(data.get("ISO_A2_EH") or data.get("ISO_A2") or "")
        code = ADMIN0_CODES.get(iso)
        if code is None:
            continue
        for polygon in _polygons(record.shape.__geo_interface__):
            shapes.setdefault(code, []).extend(polygon)

    return shapes


# --------------------------------------------------------------------------- #
#  生成
# --------------------------------------------------------------------------- #


def build_svg(shapes: dict[str, list[list[tuple[float, float]]]], tolerance: float) -> str:
    missing = sorted(set(CODE_TO_NAME) - set(shapes))
    if missing:
        names = "、".join(CODE_TO_NAME[code] for code in missing)
        raise SystemExit(f"缺少省级几何：{names}（数据源不完整，拒绝生成半个中国）")

    # 1) 投影全部点，求外接框
    projected: dict[str, list[list[tuple[float, float]]]] = {}
    xs: list[float] = []
    ys: list[float] = []
    for code, rings in shapes.items():
        out_rings: list[list[tuple[float, float]]] = []
        for ring in rings:
            if len(ring) < 3:
                continue
            points = [albers(lon, lat) for lon, lat in ring]
            if points[0] == points[-1]:
                points.pop()
            if len(points) < 3:
                continue
            out_rings.append(points)
            xs.extend(point[0] for point in points)
            ys.extend(point[1] for point in points)
        projected[code] = out_rings

    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    scale = (WIDTH - 2 * PADDING) / (max_x - min_x)
    height = round((max_y - min_y) * scale + 2 * PADDING, 1)

    def to_canvas(point: tuple[float, float]) -> tuple[float, float]:
        x, y = point
        return PADDING + (x - min_x) * scale, PADDING + (max_y - y) * scale

    # 2) 投影 → 画布坐标 → 简化 → 输出
    paths: list[str] = []
    for code in sorted(projected, key=lambda item: CODE_TO_NAME[item]):
        parts: list[str] = []
        for ring in projected[code]:
            canvas_ring = [to_canvas(point) for point in ring]
            # 闭合环交给 DP 时首尾相同会退化，先去掉尾点、简化后再补 Z。
            simplified = douglas_peucker(canvas_ring, tolerance)
            if len(simplified) < 3 or ring_area(simplified) < 0.02:
                continue
            commands = [f"M{simplified[0][0]:.1f},{simplified[0][1]:.1f}"]
            commands.extend(f"L{x:.1f},{y:.1f}" for x, y in simplified[1:])
            commands.append("Z")
            parts.append("".join(commands))
        if not parts:
            continue
        paths.append(
            f'  <path data-code="{code}" data-name="{CODE_TO_NAME[code]}" '
            f'd="{"".join(parts)}"/>'
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH:.0f} {height:.0f}"\n'
        '     role="img" aria-labelledby="china-map-title">\n'
        '  <!-- 数据源：Natural Earth 1:10m admin_1 + admin_0（public domain）\n'
        "       生成：tools/make-china-svg.py（Albers 等积圆锥 + Douglas–Peucker）\n"
        "       警告：本文件是生成产物，不要手改边界；改脚本后重新生成。 -->\n"
        '  <title id="china-map-title">中国省级行政区示意图（Natural Earth，public domain）</title>\n'
        '  <g fill="#e9edf2" stroke="#ffffff" stroke-width="1" stroke-linejoin="round"\n'
        '     vector-effect="non-scaling-stroke" fill-rule="evenodd">\n'
        + "\n".join(paths)
        + "\n  </g>\n"
        "</svg>\n"
    )


def write_preview(
    shapes: dict[str, list[list[tuple[float, float]]]],
    tolerance: float,
    path: Path,
) -> None:
    """用 Pillow 画一张 PNG（只为肉眼检查，不参与构建）。"""
    from PIL import Image, ImageDraw

    # 复用 build_svg 的投影/简化逻辑：重新算一遍坐标略笨，但脚本是一次性工具。
    projected: dict[str, list[list[tuple[float, float]]]] = {}
    xs: list[float] = []
    ys: list[float] = []
    for code, rings in shapes.items():
        out_rings = []
        for ring in rings:
            if len(ring) < 3:
                continue
            points = [albers(lon, lat) for lon, lat in ring]
            if points[0] == points[-1]:
                points.pop()
            out_rings.append(points)
            xs.extend(p[0] for p in points)
            ys.extend(p[1] for p in points)
        projected[code] = out_rings

    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    scale = (WIDTH - 2 * PADDING) / (max_x - min_x)
    height = (max_y - min_y) * scale + 2 * PADDING

    image = Image.new("RGB", (int(WIDTH * 2), int(height * 2)), "white")
    draw = ImageDraw.Draw(image)

    def to_canvas(point: tuple[float, float]) -> tuple[float, float]:
        x, y = point
        return (PADDING + (x - min_x) * scale) * 2, (PADDING + (max_y - y) * scale) * 2

    for code in sorted(projected):
        for ring in projected[code]:
            canvas_ring = [to_canvas(point) for point in ring]
            simplified = douglas_peucker(canvas_ring, tolerance * 2)
            if len(simplified) < 3 or ring_area(simplified) < 0.08:
                continue
            draw.polygon(simplified, fill=(230, 235, 241), outline=(90, 100, 115))

    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin1", type=Path, required=True, help="ne_10m_admin_1_states_provinces.shp")
    parser.add_argument("--admin0", type=Path, required=True, help="ne_10m_admin_0_countries.shp")
    parser.add_argument("--out", type=Path, required=True, help="输出 .svg")
    parser.add_argument("--preview", type=Path, help="可选：输出一张 PNG 预览")
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.45,
        help="Douglas–Peucker 容差（输出画布单位，默认 0.45 ≈ 0.3px）",
    )
    args = parser.parse_args()

    shapes = collect_shapes(args.admin1, args.admin0)
    svg = build_svg(shapes, args.tolerance)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    # 显式 LF：Windows 上 write_text 默认会把 \n 翻成 \r\n，产物哈希会因机器而异。
    args.out.write_text(svg, encoding="utf-8", newline="\n")
    print(f"svg: {args.out} ({args.out.stat().st_size:,} bytes)")

    if args.preview:
        write_preview(shapes, args.tolerance, args.preview)
        print(f"preview: {args.preview}")


if __name__ == "__main__":
    main()
