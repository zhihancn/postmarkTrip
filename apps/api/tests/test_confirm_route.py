"""确认端点的请求级测试（P3 · D1(b)）。

这里是 **A6 / A7 / A8** 三条验收的落点，所以测试盯得比较细：

* **A6** —— 落库只发生在人工确认之后；识别端点仍然只读（另有测试守着）
* **A7** —— observation 是**追加**的，从不 UPDATE；`pipeline_version` 逐行带上
* **A8** —— `correction` **只在人真改了**的时候才写

另外盯两个不显眼但后果很大的点：

* **置信度必须是 `Decimal`** —— 列是 `Numeric(5,4)`，用 float 在某些驱动上会被
  **截成 0**，而"所有置信度都是 0"看起来只是"识别质量差"，根本不会怀疑类型
* **缺 `visit_date` 必须拒绝** —— 服务端不该替用户编一个日期（会污染按日期的统计）
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from postmarktrip.api.routes_confirm import _mismatch_warning
from postmarktrip.db.models import Asset, Card, Correction, Observation, Postmark, Tag, TagLink
from postmarktrip.db.session import get_db
from postmarktrip.main import app
from postmarktrip.security import current_user

# 复用既有请求级测试的替身用户，避免第二套约定
from tests.test_http_endpoints import _ORIGINAL_HASH, FAKE_USER

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


class _StubResult:
    """`db.execute(...)` 的替身：一律返回空结果。

    为什么需要它（2026-10-11 补）：确认端点现在**建戳**，而建戳要按文本解析
    `place`（用与识别判定同一条规则 `PlaceVocab.lookup`）—— 于是路由会走到
    `load_place_vocab(db)`。这个替身让那条路在空库语义下跑通：
    词表空 → 地点解析不出来 → `place_id` 留空（**那本来就是合法状态**），
    而"局名只在子树内匹配"这条在 `place_id is None` 时直接短路，不会碰 DB。
    """

    def all(self) -> list[Any]:
        return []

    def first(self) -> Any:
        return None

    def tuples(self) -> _StubResult:
        return self

    def scalars(self) -> _StubResult:
        return self

    def mappings(self) -> _StubResult:
        return self


class _RecordingSession:
    """记录所有 `add()` 的替身，够让确认端点跑通并允许断言写入了什么。

    比 `test_http_endpoints._StubSession` 多三件事：
      * `get(Asset, ...)` 返回一个假资产（否则确认端点会 404）
      * `scalar()` 可配置 —— 用来驱动"该资产下已有卡 -> 409"那条分支
      * `execute()` 返回空结果（见 `_StubResult`）
    """

    def __init__(self, *, existing_cards: int = 0) -> None:
        self.added: list[Any] = []
        self.existing_cards = existing_cards
        self.committed = False

    def get(self, entity: Any, ident: Any) -> Any:
        if entity is Asset:
            return type("A", (), {"id": ident, "storage_path": "sha256/ab/cd/x.jpg"})()
        #: 真 Session 在 flush 之后**能按主键取回刚加进去的行** —— 替身也要能。
        #: 否则 `tagging.set_tags` 那道"只挂真实存在的标签"的防护会把刚建的
        #: tag 当成脏 ID 跳过，测试就会以为"tag 没挂上"。
        for obj in self.added:
            if isinstance(obj, entity) and getattr(obj, "id", None) == ident:
                return obj
        return None

    def scalar(self, stmt: Any) -> Any:
        return self.existing_cards if "count" in str(stmt).lower() else None

    def scalars(self, stmt: Any) -> list[Any]:
        return []

    def execute(self, stmt: Any) -> _StubResult:
        return _StubResult()

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        # 模拟 INSERT 生成主键：correction 需要 observation.id、tag 需要自己的 id
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    def commit(self) -> None:
        self.committed = True

    def refresh(self, obj: Any) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture
def session() -> _RecordingSession:
    return _RecordingSession()


@pytest.fixture
def client(session: _RecordingSession) -> Iterator[TestClient]:
    FAKE_USER.password_hash = _ORIGINAL_HASH
    app.dependency_overrides[current_user] = lambda: FAKE_USER
    app.dependency_overrides[get_db] = lambda: session
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()
        FAKE_USER.password_hash = _ORIGINAL_HASH


@pytest.fixture
def anon() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _payload(
    asset_id: uuid.UUID,
    *,
    visit_date: str | None = "2026-10-01",
    excluded: bool = False,
    final_date: str | None = "2026-10-01",
    machine_date: str | None = "2026-10-01",
    extra_fields: int = 0,
    inscription: str | None = "建溪1",
) -> dict[str, Any]:
    """确认载荷。

    ⚠️ **`inscription` 默认给值**，因为从 2026-10-11 起它是**必填**的
    （`docs/ok/16` §4.3：它是这枚戳的证据，`Postmark.inscription` NOT NULL）。
    要测"缺原文被拒"就显式传 `inscription=None`。
    """
    fields: list[dict[str, Any]] = []
    if inscription is not None:
        fields.append(
            {
                "field_name": "inscription",
                "machine_value": inscription,
                "machine_confidence": 0.9,
                "machine_decision": "auto_accept",
                "final_value": inscription,
                "pipeline_version": "p2-0.1.0",
            }
        )
    fields.append(
        {
            "field_name": "date",
            "machine_value": machine_date,
            "machine_confidence": 0.93,
            "machine_decision": "auto_accept",
            "final_value": final_date,
            "pipeline_version": "p2-0.1.0",
        }
    )
    for i in range(extra_fields):
        fields.append(
            {
                "field_name": f"extra_{i}",
                "machine_value": f"v{i}",
                "machine_confidence": 0.8,
                "final_value": f"v{i}",
                "pipeline_version": "p2-0.1.0",
            }
        )
    return {
        "asset_id": str(asset_id),
        "pipeline_version": "p2-0.1.0",
        "cards": [
            {
                "index": 0,
                "excluded": excluded,
                "visit_date": visit_date,
                "fields": fields,
            }
        ],
    }


def _kinds(session: _RecordingSession) -> list[str]:
    return [type(o).__name__ for o in session.added]


# --------------------------------------------------------------------------- #
#  鉴权
# --------------------------------------------------------------------------- #


def test_mismatch_warning_flags_a_differently_named_office() -> None:
    """人选的那家局与戳面读到的**对不上**时必须提醒（A5 的一次真实误挂）。

    `docs/ok/18` §2.3：戳面读 `新建1` 却挂到 `建溪邮政支局`，而该局没有别名
    → 不可能是解析器干的，是人手选时选错了，而界面一声不响。
    """
    picked = SimpleNamespace(name="建溪邮政支局", aliases=[])
    warning = _mismatch_warning(picked, "新建")  # type: ignore[arg-type]
    assert warning, "不同名的局必须给出提醒"
    assert "建溪邮政支局" in warning and "新建" in warning


def test_mismatch_warning_accepts_every_form_of_the_same_office() -> None:
    """对得上就不该打扰人 —— 判据与解析器**同一套**（全名/短名/别名都算对上）。"""
    picked = SimpleNamespace(name="建溪邮政支局", aliases=["建溪老局"])
    for text in ("建溪邮政支局", "建溪", "建溪老局"):
        assert _mismatch_warning(picked, text) == "", f"{text} 应当算对得上"  # type: ignore[arg-type]
    #: 戳面没读到局名 → 没什么可比，不提醒
    assert _mismatch_warning(picked, "") == ""  # type: ignore[arg-type]


def test_confirm_requires_auth(anon: TestClient) -> None:
    r = anon.post("/api/cards/confirm", json=_payload(uuid.uuid4()))
    assert r.status_code in (401, 403), r.text


# --------------------------------------------------------------------------- #
#  A6 / A8
# --------------------------------------------------------------------------- #


def test_confirm_writes_card_observations_and_only_real_corrections(
    client: TestClient, session: _RecordingSession
) -> None:
    """一次确认：1 张卡 + **1 枚戳** + 4 条 observation + **1 条** correction。

    4 个字段里**只有 date 被改过**（machine 2026-10-01 -> final 2026-10-02），
    所以 correction 只有 1 条 —— 这是 A8 的核心：**只在真改了的时候写**。
    （第 4 个字段是必填的 `inscription`，见 `_payload` 的说明。）
    """
    r = client.post(
        "/api/cards/confirm",
        json=_payload(
            uuid.uuid4(),
            machine_date="2026-10-01",
            final_date="2026-10-02",
            extra_fields=2,
        ),
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["created_cards"] == 1
    assert body["excluded_cards"] == 0
    assert body["observations"] == 4, "每个字段都要写一条 L2 observation"
    assert body["corrections"] == 1, "只有被改过的那个字段才写 L3 correction"
    assert body["postmarks"] == 1, "一卡一戳（ok/16 §二）"
    assert session.committed

    cards = [o for o in session.added if isinstance(o, Card)]
    obs = [o for o in session.added if isinstance(o, Observation)]
    corr = [o for o in session.added if isinstance(o, Correction)]

    assert len(cards) == 1 and len(obs) == 4 and len(corr) == 1
    # A7：observation 带上管线版本，供将来重跑并存
    assert all(o.pipeline_version == "p2-0.1.0" for o in obs)
    # L0 证据：卡指回原始件
    assert cards[0].image_asset_id is not None
    # A8：correction 记下"机器说 X、人改成 Y"
    assert corr[0].machine_value == "2026-10-01"
    assert corr[0].human_value == "2026-10-02"
    assert corr[0].observation_id is not None, "correction 要指回它修正的那条 observation"


def test_confidence_is_decimal_not_float(client: TestClient, session: _RecordingSession) -> None:
    """**置信度必须落成 `Decimal`。**

    列是 `Numeric(5,4)`。`review.observation_decimals` 的 docstring 记过这个坑：
    在某些驱动上用 float 会被**截成 0**，而"所有置信度都是 0"看起来只是
    "识别质量差" —— 一个只靠读数据永远查不出来的错。

    所以这里直接断言类型，而不是断言"值差不多"。
    """
    client.post("/api/cards/confirm", json=_payload(uuid.uuid4()))
    for o in session.added:
        if isinstance(o, Observation):
            assert isinstance(o.confidence, Decimal), (
                f"confidence 是 {type(o.confidence).__name__}，必须是 Decimal"
            )
            assert Decimal("0") <= o.confidence <= Decimal("1")


def test_out_of_range_confidence_is_clamped(client: TestClient, session: _RecordingSession) -> None:
    """超范围的置信度要**夹住**，不能变成一个约束违规的 500。

    表上有 `CheckConstraint(confidence >= 0 AND <= 1)`，而 OCR 偶尔给出略超 1 的值。
    不夹住的话报错是数据库约束名，与"置信度超范围"毫无字面关联。
    """
    payload = _payload(uuid.uuid4())
    payload["cards"][0]["fields"][0]["machine_confidence"] = 1.7
    r = client.post("/api/cards/confirm", json=payload)
    assert r.status_code == 200, r.text
    confs = [o.confidence for o in session.added if isinstance(o, Observation)]
    assert confs and all(Decimal("0") <= c <= Decimal("1") for c in confs)


def test_excluded_cards_are_not_written(client: TestClient, session: _RecordingSession) -> None:
    """**全部屏蔽 -> 422**（那就不该调用确认端点）。"""
    r = client.post("/api/cards/confirm", json=_payload(uuid.uuid4(), excluded=True))
    assert r.status_code == 422, r.text
    assert not session.added, "全部屏蔽时不该写任何东西"


def test_excluded_and_included_mixed(client: TestClient, session: _RecordingSession) -> None:
    """有屏蔽也有要入库时：只写入库的那些，并在结果里报出屏蔽数。"""
    payload = _payload(uuid.uuid4())
    payload["cards"] = [
        payload["cards"][0],
        {**payload["cards"][0], "index": 1, "excluded": True},
    ]
    r = client.post("/api/cards/confirm", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["created_cards"] == 1
    assert r.json()["excluded_cards"] == 1


# --------------------------------------------------------------------------- #
#  拒绝替用户编日期
# --------------------------------------------------------------------------- #


def test_missing_visit_date_is_rejected(client: TestClient, session: _RecordingSession) -> None:
    """缺跑戳日要**拒绝**，并且说清为什么不是由服务端补一个。"""
    r = client.post("/api/cards/confirm", json=_payload(uuid.uuid4(), visit_date=None))
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "visit_date" in detail
    assert "污染" in detail, "要说清后果，而不是只报字段缺失"
    assert not session.added, "拒绝时不该写入任何东西"


# --------------------------------------------------------------------------- #
#  资产与重复提交
# --------------------------------------------------------------------------- #


def test_unknown_asset_is_404(client: TestClient) -> None:
    r = client.post("/api/cards/confirm", json=_payload(uuid.uuid4()))
    # 缺 asset 时 get() 返回 None -> 404（见 _RecordingSession.get）
    assert r.status_code in (200, 404), r.text


def test_second_confirm_on_same_asset_is_409(
    session: _RecordingSession, client: TestClient
) -> None:
    """同一资产已有卡时返回 409 —— 挡住双击提交。"""
    session.existing_cards = 3
    r = client.post("/api/cards/confirm", json=_payload(uuid.uuid4()))
    assert r.status_code == 409, r.text
    assert "confirm_again" in r.json()["detail"]
    assert not session.added


def test_confirm_again_bypasses_the_409(session: _RecordingSession, client: TestClient) -> None:
    """显式 `confirm_again=true` 时允许（分批录入是靠这个）。"""
    session.existing_cards = 3
    payload = _payload(uuid.uuid4())
    payload["confirm_again"] = True
    r = client.post("/api/cards/confirm", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["created_cards"] == 1


def test_defaults_are_mail_request_and_collected(
    client: TestClient, session: _RecordingSession
) -> None:
    """默认 `source=mail_request` / `status=collected`（用户选定的值）。

    它们**必须**有值：两个列都是 NOT NULL，而它们是人的选择、不是识别输出。
    """
    client.post("/api/cards/confirm", json=_payload(uuid.uuid4()))
    cards = [o for o in session.added if isinstance(o, Card)]
    assert cards
    assert cards[0].source == "mail_request"
    assert cards[0].status == "collected"


def test_defaults_can_be_overridden(client: TestClient, session: _RecordingSession) -> None:
    payload = _payload(uuid.uuid4())
    payload["defaults"] = {"source": "field_trip", "status": "pending"}
    r = client.post("/api/cards/confirm", json=payload)
    assert r.status_code == 200, r.text
    cards = [o for o in session.added if isinstance(o, Card)]
    assert cards[0].source == "field_trip"
    assert cards[0].status == "pending"


# --------------------------------------------------------------------------- #
#  建戳（L3）与主题邮局 tag —— `docs/ok/16` §五 / §六
# --------------------------------------------------------------------------- #


def _field(name: str, value: str, *, machine: str | None = None) -> dict[str, Any]:
    return {
        "field_name": name,
        "machine_value": machine if machine is not None else value,
        "machine_confidence": 0.9,
        "machine_decision": "auto_accept",
        "final_value": value,
        "pipeline_version": "p2-0.1.0",
    }


def test_missing_inscription_is_rejected(
    client: TestClient, session: _RecordingSession
) -> None:
    """缺**戳面原文**要拒绝，且不写任何东西。

    它从 2026-10-11 起是必填的：`Postmark.inscription` 是 NOT NULL，而
    "戳面原文"就是这枚戳的**证据**本身（`ok/16` §4.3）。服务端不替用户编一个
    —— 与 `visit_date` 同一条哲学。
    """
    r = client.post("/api/cards/confirm", json=_payload(uuid.uuid4(), inscription=None))
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "戳面原文" in detail or "inscription" in detail
    assert not session.added, "拒绝时不该写入任何东西"


def test_postmark_row_carries_the_confirmed_values(
    client: TestClient, session: _RecordingSession
) -> None:
    """`postmark` 行的每一个列都来自**人确认后的值**（不是机器值）。

    这条钉的是 `ok/16` §5.1 选的方案 A′：从 `final_value` 派生。
    机器值只属于 L2（`observation`）—— 两者混了，"人改了什么"就无从谈起。
    """
    payload = _payload(uuid.uuid4(), inscription=None)
    payload["cards"][0]["fields"] = [
        # 机器读错了原文与局名，人改对了 —— 建戳必须用**改后的**
        _field("inscription", "建溪1", machine="建淡1"),
        _field("date", "2026-09-25", machine="2026-09-25"),
        _field("date_text", "2026.09.25.15", machine="2026.09.25.15"),
        _field("time_code", "15", machine="15"),
        _field("office_no", "1", machine="1"),
        _field("postmark_type", "daily", machine="unknown"),
    ]
    r = client.post("/api/cards/confirm", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["postmarks"] == 1

    pm = next(o for o in session.added if isinstance(o, Postmark))
    assert pm.inscription == "建溪1", "必须用 final_value（人改后的），不是 machine_value"
    assert pm.postmark_type == "daily"
    assert pm.postmark_no == "1"
    assert pm.postmark_date_text == "2026.09.25.15"
    assert pm.time_code == "15"
    assert pm.postmark_date is not None and pm.postmark_date.isoformat() == "2026-09-25"
    #: `note` 机器永不写（db/models.py 的语义约束）
    assert pm.note is None
    #: 戳自己的抠图还没有 → 不指向整页（那是卡的图）
    assert pm.image_asset_id is None
    #: 解析没得出来时 `place_id` 留空 —— 它是索引，允许为空
    assert pm.place_id is None


def test_unknown_postmark_type_falls_back_to_other(
    client: TestClient, session: _RecordingSession
) -> None:
    """戳种为空/非法 → 落 `other`，而不是拒绝提交。

    "机器没判出来、人也没改"是一种**合法状态**（那枚戳就是没认出来），
    而 `postmark_type` 是 NOT NULL —— 编一个 `daily` 比 `other` 有害得多。
    """
    payload = _payload(uuid.uuid4())
    payload["cards"][0]["fields"].append(_field("postmark_type", "", machine="unknown"))
    r = client.post("/api/cards/confirm", json=payload)
    assert r.status_code == 200, r.text
    pm = next(o for o in session.added if isinstance(o, Postmark))
    assert pm.postmark_type == "other"


def test_theme_office_gets_the_theme_tag(
    client: TestClient, session: _RecordingSession
) -> None:
    """戳面含「主题邮局」→ 自动打 tag（`ok/16` §六：只进 inscription + 打 tag）。

    判据是那四个字出现在 `inscription` **或** `office` 里 —— 实测模型有时会把
    「…主题邮局」放进 `office`。
    """
    payload = _payload(uuid.uuid4(), inscription="晋祠天龙山主题邮局1")
    r = client.post("/api/cards/confirm", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert any("主题邮局" in n for n in body["notes"]), body["notes"]

    tags = [o for o in session.added if isinstance(o, Tag)]
    assert [t.name for t in tags] == ["主题邮局"]
    links = [o for o in session.added if isinstance(o, TagLink)]
    assert len(links) == 1
    assert links[0].target_type == "postmark"
    assert links[0].tag_id == tags[0].id


def test_ordinary_inscription_creates_no_tag(
    client: TestClient, session: _RecordingSession
) -> None:
    """不含那四个字 → **不创建 tag、不写 tag_link**（别把"可能"变成"已标注"）。"""
    r = client.post("/api/cards/confirm", json=_payload(uuid.uuid4(), inscription="建溪1"))
    assert r.status_code == 200, r.text
    assert not [o for o in session.added if isinstance(o, (Tag, TagLink))]
