"""识别路由的请求级测试（P3 阶段 0）。

这些测试盯的是**契约**，不是识别准确率（准确率在 `tools/samples/` 与
`ok/10` 的报告里）。具体盯五件事：

1. **不写库** —— spec A6 要求"所有自动结果无一例外经人工确认才落库"。
   本端点靠**结构**保证这一点（它根本不接受 `db` 依赖），
   下面的 `test_recognize_route_has_no_db_dependency` 就是在钉这个结构。
2. **每个字段都带 `decision`** —— UI 上色的唯一依据是决策而不是置信度
   （实测「福建南火」置信度 0.90 却是错的）。
3. **几何信息回传** —— `quad` 与 `circle` 让"切得准不准/圆找得对不对"
   在界面上一眼可见。这两种错是**最难从字段值看出来**的失效模式。
4. **云端默认关** —— 云端花钱且结果不可复现，必须显式打开。
5. **识别默认关，且"未启用"与"依赖没装"要给不同的消息** ——
   前者改配置、后者装包，合成一句话会叫人做错事。
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from postmarktrip.api import routes_recognize
from postmarktrip.config import settings
from postmarktrip.db.session import get_db
from postmarktrip.main import app
from postmarktrip.security import current_user

# 复用既有请求级测试的替身与用户，避免第二套约定
from tests.test_http_endpoints import _ORIGINAL_HASH, FAKE_USER, _StubSession


def _probe() -> Any:
    from postmarktrip.providers import probe_cv, probe_ocr

    return probe_cv(), probe_ocr()


def _ready() -> bool:
    """当前配置下识别是否**真的可用**。

    ⚠️ 必须在**把引擎打开之后**再探测：``probe_ocr()`` 在
    ``ocr_engine="none"`` 时返回 ``enabled=False, available=False``
    （这是有意的设计），所以"先探再开"会让所有用例都被跳过 ——
    那看起来像"环境没装"，其实是探针问错了时机（第一次就踩了这个坑）。
    """
    cv, ocr = _probe()
    return bool(cv.ready and ocr.ready)


pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture
def client() -> Iterator[TestClient]:
    FAKE_USER.password_hash = _ORIGINAL_HASH
    app.dependency_overrides[current_user] = lambda: FAKE_USER
    app.dependency_overrides[get_db] = lambda: _StubSession()
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()
        FAKE_USER.password_hash = _ORIGINAL_HASH


@pytest.fixture
def anon() -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


@pytest.fixture
def enabled(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """把 OCR 引擎打开（默认是 ``none``）。

    识别默认关闭是有意的设计（见 `config.ocr_engine` 的说明：可选依赖不该在
    升级后自动开始跑）。所以要测识别本身，必须先显式打开 —— 与真实部署一致。
    """
    monkeypatch.setattr(settings, "ocr_engine", "rapidocr")
    if not _ready():
        pytest.skip("识别依赖（recognition extra）未安装")
    return client


#: 合成图里一张白卡的像素尺寸。**dpi 必须与它自洽**，否则切分器会
#: 一个候选都找不到（期望单卡面积按 dpi 折算），然后降级成"整页当作单卡"
#: —— 那样测到的就不是多卡切分路径了（第一版测试就踩了这个）。
_CARD_PX = (240, 400)  # 宽, 高


def _synthetic_dpi() -> int:
    """由合成几何反推 dpi：卡宽 240px 对应 54mm。"""
    return round(_CARD_PX[0] / 54 * 25.4)


def _page_png(*, cards: int = 2) -> bytes:
    """一张深色垫底、带 N 张白卡的合成扫描图（PNG 字节）。"""
    import cv2
    import numpy as np

    cw, ch, gap = 240, 400, 40
    page = np.full((ch + 2 * gap, cards * cw + (cards + 1) * gap, 3), 40, np.uint8)
    for i in range(cards):
        x = gap + i * (cw + gap)
        card = np.full((ch, cw, 3), 250, np.uint8)
        # 邮戳尺度的暗环（12.5mm / 54mm ≈ 0.23 卡宽）
        cv2.circle(card, (cw // 2, ch // 2 + 40), int(cw * 0.23), (30, 30, 30), 3)
        page[gap : gap + ch, x : x + cw] = card
    ok, buf = cv2.imencode(".png", page)
    assert ok
    return bytes(buf.tobytes())


# --------------------------------------------------------------------------- #
#  鉴权
# --------------------------------------------------------------------------- #


def test_recognize_requires_auth(anon: TestClient) -> None:
    """识别不是参考数据，必须登录。"""
    r = anon.post("/api/recognize", files={"file": ("x.png", b"\x89PNG", "image/png")})
    assert r.status_code in (401, 403), r.text


# --------------------------------------------------------------------------- #
#  结构：不写库（A6 靠结构保证，而不是靠自觉）
# --------------------------------------------------------------------------- #


#: 会写库的调用。识别端点里出现任何一个都是 A6 违规。
WRITE_MARKERS = (
    "db.add(",
    "db.add_all(",
    "db.commit(",
    "db.flush(",
    "db.delete(",
    ".commit()",
    "Observation(",
    "Correction(",
)


def test_recognize_route_is_read_only() -> None:
    """**本端点必须只读** —— 这是 spec A6 的结构性保证。

    A6：「所有自动结果**无一例外**经过人工确认才落库」。
    落库发生在用户确认之后的另一个端点（`/cards/confirm`）。

    ⚠️ 这条断言**改过一次，而且必须记下来为什么**：

    最初写的是"**不接受任何 db 依赖**" —— 那是更强的保证（没有会话就不可能写）。
    但方案 A 要求本端点按 `asset_id` **读出**原始图，所以必须有 db 会话。
    **读不是写**，A6 关心的是写。

    所以断言从"没有会话"精确到"**没有任何写操作**"。**这不是为了让它通过而放宽** ——
    它仍然会拦住"顺手把 observation 存一下"这种改动，而那正是本条测试存在的理由。
    """
    names = set(inspect.signature(routes_recognize.recognize_page).parameters)
    assert "db" in names, "方案 A 下本端点需要读资产，应当有 db 依赖"
    assert "store" in names, "读原图还需要 store"

    src = inspect.getsource(routes_recognize)
    hits = [m for m in WRITE_MARKERS if m in src]
    assert not hits, (
        f"识别端点里出现了写库调用 {hits} —— A6 要求识别阶段不落库，落库应在 /cards/confirm 里发生"
    )


def test_recognize_module_does_not_build_observation_rows() -> None:
    """不得构造 `Observation` / `Correction` —— 防止"间接"写库。"""
    src = inspect.getsource(routes_recognize)
    for forbidden in ("Observation", "Correction", "review.build_bundle"):
        assert forbidden not in src, f"识别端点不应涉及 {forbidden}"


# --------------------------------------------------------------------------- #
#  未启用 vs 依赖没装：两种 503，两种消息
# --------------------------------------------------------------------------- #


def test_disabled_engine_says_disabled_not_missing_deps(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``OCR_ENGINE=none``（默认）时，消息必须是"未启用"。

    第一版把这种情况说成"识别依赖未安装" —— 那会叫用户去装一个
    **已经装好的包**，而真正要做的是改一个环境变量。两种消息必须分开。
    """
    monkeypatch.setattr(settings, "ocr_engine", "none")
    r = client.post("/api/recognize", files={"file": ("p.png", _page_png(), "image/png")})
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert "未启用" in detail, detail
    assert "PMTRIP_OCR_ENGINE" in detail, "要告诉用户改哪个开关"
    assert "未安装" not in detail, "这是配置问题，不是安装问题"


# --------------------------------------------------------------------------- #
#  输入校验
# --------------------------------------------------------------------------- #


def test_recognize_rejects_empty_file(enabled: TestClient) -> None:
    r = enabled.post("/api/recognize", files={"file": ("e.png", b"", "image/png")})
    assert r.status_code == 422, r.text


def test_recognize_rejects_non_image(enabled: TestClient) -> None:
    """不是图片要 415，而不是 500 或一个空结果。"""
    r = enabled.post(
        "/api/recognize", files={"file": ("x.txt", b"not an image at all", "text/plain")}
    )
    assert r.status_code == 415, r.text


def test_recognize_validates_long_side(enabled: TestClient) -> None:
    r = enabled.post(
        "/api/recognize?long_side=10",
        files={"file": ("x.png", _page_png(), "image/png")},
    )
    assert r.status_code == 422, r.text


# --------------------------------------------------------------------------- #
#  契约
# --------------------------------------------------------------------------- #


def test_recognize_returns_cards_with_decisions(enabled: TestClient) -> None:
    """核心契约：返回卡片，且**每个字段都带 decision**。

    UI 上色只能依据 ``decision``。若这里退化成只有 ``confidence``，
    界面就会把「福建南火」（置信度 0.90）显示成可信。
    """
    r = enabled.post(
        f"/api/recognize?dpi={_synthetic_dpi()}",
        files={"file": ("p.png", _page_png(), "image/png")},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["card_count"] >= 1
    assert body["pipeline_version"], "必须回传 pipeline_version（observation 要用它）"
    assert body["used_vlm"] is False, "云端默认必须是关的"

    allowed = {"auto_accept", "needs_review", "reject"}
    for card in body["cards"]:
        for f in card["fields"]:
            assert f["decision"] in allowed, f"未知决策：{f['decision']}"
            assert f["reason"], "每个决策都要给出理由（界面直接显示）"
            assert f["pipeline_version"], "字段要带 pipeline_version"


def test_recognize_returns_geometry_for_the_ui(enabled: TestClient) -> None:
    """几何必须回传：UI 要画**实际的**四边形，而不是轴对齐的框。

    卡在扫描时可能倾斜（实测 `更多样本/005.jpg` 底行就有），
    用 bbox 画会与实际边界差很多。
    """
    r = enabled.post(
        f"/api/recognize?dpi={_synthetic_dpi()}",
        files={"file": ("p.png", _page_png(), "image/png")},
    )
    body = r.json()
    card = body["cards"][0]
    assert len(card["quad"]) == 4, "四边形应有 4 个顶点"
    for x, y in card["quad"]:
        assert isinstance(x, float) and isinstance(y, float)
    assert card["card_size"][0] > 0 and card["card_size"][1] > 0


def test_recognize_reports_the_structured_links_it_would_attach(
    enabled: TestClient,
) -> None:
    """每张卡要回传**结构化地点/邮局的自动解析结果**（`docs/ok/18` §3.3）。

    用户 2026-10-12："尽管不填【结构化】，只要能在【局名】【地名】命中，
    就能补充到结构化字段上面去"。这件事确认时本来就在做 —— 但界面看不见，
    于是 A5 里手填了 14 次结构化选择（大多本来就解析得出来）。

    这条测试钉**契约在**（字段必须出现在响应里，哪怕是空的）——
    值是空的也算通过，因为合成页面上本来就没有可读的文字。
    """
    r = enabled.post(
        f"/api/recognize?dpi={_synthetic_dpi()}",
        files={"file": ("p.png", _page_png(), "image/png")},
    )
    body = r.json()
    for card in body["cards"]:
        assert "links" in card, "每张卡都要有 links（界面靠它显示/预填）"
        links = card["links"]
        assert set(links) == {
            "place_id",
            "place_name",
            "office_id",
            "office_name",
            "office_place_name",
            "note",
        }
        #: 这个端点仍然**只读**（spec A6）—— links 只是"会挂到哪儿"的预告
        assert links["place_id"] is None or isinstance(links["place_id"], str)


def test_with_links_serialises_the_resolution_for_the_ui() -> None:
    """`_with_links` 必须能把解析结果**装进响应模型**（2026-10-12 实测 500 的回归）。

    ⚠️ 上面那条路由测试抓不到这个 bug：合成页面上没有可读文字 →
    `_with_links` 走"两个文本都空"的提前返回分支，**永远碰不到**
    `CardLinksRead.model_validate(...)`。而真实样本一定会碰到
    （`postmark_assembly.CardLinks` 是 dataclass，没有 `from_attributes`
    就会以 `model_type` 报错 → 接口 500）。
    """
    import uuid as _uuid
    from types import SimpleNamespace

    from postmarktrip.api.routes_recognize import _with_links
    from postmarktrip.domain.schemas import RecognizedCardRead, RecognizedFieldRead

    place_id, office_id = _uuid.uuid4(), _uuid.uuid4()
    place = SimpleNamespace(id=place_id, name="玉山县", path="/CN/p/c/")
    office = SimpleNamespace(id=office_id, name="三清山邮政所", place_id=place_id)
    rows = [SimpleNamespace(id=office_id, name="三清山邮政所", aliases=[])]

    class _Db:
        def get(self, entity: object, ident: object) -> object:
            return place if ident == place_id else office

        def execute(self, stmt: object) -> object:
            return SimpleNamespace(all=lambda: rows)

    class _Vocab:
        def lookup(self, text: str) -> object:
            return SimpleNamespace(
                known=True,
                hit=SimpleNamespace(place_id=place_id, path=("中华人民共和国", "江西省", "玉山县")),
            )

    card = RecognizedCardRead(
        index=0,
        bbox=(0, 0, 10, 10),
        card_size=(10, 10),
        fields=[
            RecognizedFieldRead(
                field_name="place",
                value="江西玉山",
                confidence=0.9,
                decision="auto_accept",
                reason="",
                pipeline_version="p2-0.1.0",
            ),
            RecognizedFieldRead(
                field_name="office",
                value="三清山",
                confidence=0.9,
                decision="auto_accept",
                reason="",
                pipeline_version="p2-0.1.0",
            ),
        ],
    )
    out = _with_links(_Db(), _Vocab(), card)  # type: ignore[arg-type]
    assert out.links.place_id == place_id
    assert out.links.place_name == "玉山县"
    assert out.links.office_id == office_id
    assert out.links.office_name == "三清山邮政所"
    assert out.links.office_place_name == "玉山县"
    assert "地点：" in out.links.note and "局名：" in out.links.note


def test_recognize_circle_reports_scale_ratio(enabled: TestClient) -> None:
    """圆的 ``radius_over_card_width`` 是**排障信号**，不只是画图用。

    正常邮戳约 0.23 卡宽。实测有一个失效样本的圆落在明信片印刷抬头上，
    半径接近 0.5 卡宽（卡尺度误检）—— 有这一列，那种错一眼可见。
    """
    r = enabled.post(
        f"/api/recognize?dpi={_synthetic_dpi()}",
        files={"file": ("p.png", _page_png(), "image/png")},
    )
    body = r.json()
    circles = [c["circle"] for c in body["cards"] if c["circle"]]
    for circle in circles:
        assert circle["r"] > 0
        assert 0 < circle["radius_over_card_width"] < 2.0


def test_recognize_stage_notes_are_exposed(enabled: TestClient) -> None:
    """降级/异常说明要暴露出来（**不静默**）。"""
    r = enabled.post(
        f"/api/recognize?dpi={_synthetic_dpi()}",
        files={"file": ("p.png", _page_png(), "image/png")},
    )
    body = r.json()
    assert isinstance(body["stage_notes"], list)
    assert body["stage_notes"], "至少要说明用了什么 DPI / 切分级"


def test_circle_lies_inside_its_own_card(enabled: TestClient) -> None:
    """**戳圆必须落在它自己那张卡的框内。**

    这条不变量看起来显然，但它抓到过一个真 bug：`verdict.circle` 是
    **卡局部**坐标（classify_postmark 跑在正射图上），而界面把它画在
    **整页**上 —— 于是一页 7 张卡的圆**全部重叠在页面左上角**。

    那个错**不报错**，只是看起来"圆检测很乱"，很容易被误判成检测器不好。
    所以这里把不变量钉住：圆在自己卡的 bbox 内（允许小量外扩，因为
    邮政戳可以压到卡边）。
    """
    r = enabled.post(
        f"/api/recognize?dpi={_synthetic_dpi()}",
        files={"file": ("p.png", _page_png(cards=2), "image/png")},
    )
    assert r.status_code == 200, r.text

    checked = 0
    for card in r.json()["cards"]:
        circle = card["circle"]
        if not circle:
            continue
        checked += 1
        x, y, w, h = card["bbox"]
        pad = max(w, h) * 0.25  # 允许戳压到卡边之外一点
        assert x - pad <= circle["cx"] <= x + w + pad, (
            f"圆的 cx={circle['cx']} 落在卡 bbox x={x}..{x + w} 之外 —— "
            f"说明坐标空间没统一（卡局部 vs 页面）"
        )
        assert y - pad <= circle["cy"] <= y + h + pad, (
            f"圆的 cy={circle['cy']} 落在卡 bbox y={y}..{y + h} 之外"
        )
    assert checked >= 1, "这个用例应当至少定位到一个圆，否则不变量没被真正检验"


def test_recognize_requires_asset_or_file(enabled: TestClient) -> None:
    """两个入口都不给时要明确报错，而不是一个空结果。

    用 `enabled`（先把引擎打开）是因为**能力门禁在输入校验之前** ——
    这是刻意的：服务被关掉时，"识别未启用"比"你必须给 asset_id"更有用，
    因为改请求也不会让它工作。
    """
    r = enabled.post("/api/recognize")
    assert r.status_code == 422, r.text
    assert "asset_id" in r.json()["detail"]


def test_recognize_unknown_asset_is_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不存在的 asset_id 要 404，而不是 500 或"解码失败"。

    区分很重要：404 说明"没有这个资产"，而"解码失败"会让人去查图片格式。
    """
    monkeypatch.setattr(settings, "ocr_engine", "rapidocr")
    r = client.post(f"/api/recognize?asset_id={uuid.uuid4()}")
    assert r.status_code == 404, r.text
    assert "资产不存在" in r.json()["detail"]
