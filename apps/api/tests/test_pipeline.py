"""End-to-end pipeline tests (spec 3.2-3.8).

Before ``pipeline.py`` existed, every stage was individually tested but
nothing proved the chain was still connected. A change in any stage's
signature could break the chain silently -- the individual tests would all
still pass.

These tests use synthetic pages so they need no samples and no OCR model:
the OCR engine is allowed to be unavailable, and the pipeline must still
produce a well-formed result with the failure visible.

NOTE: ASCII-only docstrings -- see docs/ok/09-P2-non-code-errors.md E-08.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from postmarktrip.recognition.fields import Field, ParsedFields
from postmarktrip.recognition.pipeline import (
    PIPELINE_VERSION,
    _merge_fallback,
    observations_for_card,
    recognize_card,
    recognize_page,
)

# --------------------------------------------------------------------------- #
#  Synthetic pages
# --------------------------------------------------------------------------- #

#: Nominal DPI for the synthetic cards.
#:
#: A 180x300px card at 85dpi is 54x90mm -- i.e. exactly a real card. Using
#: 600dpi instead makes the same card 7.6x12.7mm, so the splitter's area
#: filter rejects it and the postmark-circle search range is 7x too wide.
#: The synthetic geometry must be self-consistent with the nominal DPI or
#: the tests fail for reasons unrelated to what they check.
SYNTH_DPI = 85

PAPER = 255
DARK_PAD = 40
W, H = 900, 700


def daily_card(h: int = 300, w: int = 180) -> np.ndarray:
    """A plausible card: white body + a postmark-like circular stamp."""
    card = np.full((h, w, 3), 250, np.uint8)
    cx, cy = w // 2, h // 2
    r = min(h, w) // 4
    cv2.circle(card, (cx, cy), r, (30, 30, 30), 4)
    cv2.circle(card, (cx, cy), int(r * 0.62), (60, 60, 60), 2)
    cv2.putText(card, "A", (cx - 8, cy + 8), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (40, 40, 40), 2)
    return card


def dark_pad_page(*, cards: int = 2, card_px: tuple[int, int] = (300, 180)) -> np.ndarray:
    ch, cw = card_px
    gap = 40
    cols = min(cards, 2)
    rows = (cards + cols - 1) // cols
    page = np.full(
        (rows * ch + (rows + 1) * gap, cols * cw + (cols + 1) * gap, 3), DARK_PAD, np.uint8
    )
    for i in range(cards):
        r, c = divmod(i, cols)
        y = gap + r * (ch + gap)
        x = gap + c * (cw + gap)
        page[y : y + ch, x : x + cw] = daily_card(ch, cw)
    return page


# --------------------------------------------------------------------------- #
#  Single card
# --------------------------------------------------------------------------- #


def test_recognize_card_returns_a_well_formed_result() -> None:
    """Every field of CardResult must be populated consistently.

    The point is not that the values are correct (there is no real text on
    a synthetic card) but that the chain ran and produced a coherent shape.
    """
    res = recognize_card(daily_card(), dpi=SYNTH_DPI)
    assert res.card_size == (180, 300)
    assert res.verdict is not None, "the type verdict stage did not run"
    assert res.postmark_type in ("daily", "scenic", "unknown")
    assert res.bundle is not None, "the review stage did not run"


def test_card_without_a_circle_still_produces_a_bundle() -> None:
    """A blank card must not crash and must not silently return None.

    Downstream (the review queue) needs an observation set for every card,
    including the ones where nothing was found -- otherwise a card with no
    postmark simply disappears from the pipeline output.
    """
    blank = np.full((300, 180, 3), 255, np.uint8)
    res = recognize_card(blank, dpi=SYNTH_DPI)
    assert res.bundle is not None
    assert res.notes, "a card with no postmark must carry an explanatory note"
    # Observations still exist, all rejected
    rows = observations_for_card(res)
    assert rows, "no observations produced for a postmark-less card"
    assert all(r["pipeline_version"] == PIPELINE_VERSION for r in rows)


def test_degradation_is_recorded_not_swallowed() -> None:
    """Failures must appear in notes.

    The pipeline is designed never to raise: one unreadable stamp must not
    fail a page containing eight good ones. But "never raise" is only safe
    if the failure is structurally visible, so every early return appends a
    note.
    """
    blank = np.full((300, 180, 3), 255, np.uint8)
    res = recognize_card(blank)
    assert res.notes, "degradation was swallowed silently"


def test_observations_cover_the_expected_fields() -> None:
    """date/place/office observations must always exist, even when missed."""
    res = recognize_card(daily_card(), dpi=SYNTH_DPI)
    rows = observations_for_card(res)
    names = {r["field_name"] for r in rows}
    assert {"date", "place", "office"} <= names, f"missing fields in {names}"


def test_observation_rows_are_json_friendly() -> None:
    """Rows go into JSONB columns, so they must be serialisable.

    ``Decimal`` is deliberately kept (Numeric(5,4) needs it) but that means
    the caller needs to know -- so it is asserted here rather than
    discovered later when a DB write fails.
    """
    import json
    from decimal import Decimal

    res = recognize_card(daily_card(), dpi=SYNTH_DPI)
    rows = observations_for_card(res)
    for row in rows:
        payload = {k: str(v) if isinstance(v, Decimal) else v for k, v in row.items()}
        json.dumps(payload)  # must not raise


def test_bbox_is_recorded_on_observations_for_the_review_ui() -> None:
    """The review UI draws where the value came from, so bbox is required."""
    res = recognize_card(daily_card(), dpi=SYNTH_DPI)
    rows = observations_for_card(res)
    assert any(r["bbox"] for r in rows), "no bbox on any observation"


# --------------------------------------------------------------------------- #
#  Whole page
# --------------------------------------------------------------------------- #


def test_recognize_page_splits_and_processes_every_card() -> None:
    """The chain must run per card, not just for the first one."""
    page = dark_pad_page(cards=2)
    res = recognize_page(page, dpi=SYNTH_DPI)
    assert res.card_count == 2, f"expected 2 cards, got {res.card_count}: {res.stage_notes}"
    for card in res.cards:
        assert card.bundle is not None
        assert card.verdict is not None


def test_page_result_reports_the_work_scale() -> None:
    """The work scale must be reported: every pixel-valued criterion is
    computed at that scale, so a later discrepancy is uninterpretable
    without it."""
    big = cv2.resize(dark_pad_page(cards=1), (4000, 3000), interpolation=cv2.INTER_CUBIC)
    res = recognize_page(big, long_side=1000)
    assert res.work_scale < 1.0
    assert res.work_size[0] <= 1000 + 1


def test_page_notes_explain_the_dpi_decision() -> None:
    """DPI determines the postmark-circle search range and every mm<->px
    conversion, so how it was chosen must be visible."""
    res = recognize_page(dark_pad_page(cards=1), dpi=SYNTH_DPI)
    assert any("DPI" in n for n in res.stage_notes), res.stage_notes


def test_fallback_dpi_is_flagged_when_unknown() -> None:
    """When DPI cannot be inferred, the result must say so.

    Measured: the close-up samples have an effective DPI of about 360, not
    600, which makes every mm figure wrong by the same factor. Silently
    using a fallback would present wrong physical sizes as facts.
    """
    # A page with no card-like region -> split cannot infer DPI
    blank = np.full((600, 600, 3), 200, np.uint8)
    res = recognize_page(blank)
    joined = " ".join(res.stage_notes)
    assert "DPI" in joined
    assert "兜底" in joined or "不可信" in joined, (
        f"fallback DPI was used without a warning: {res.stage_notes}"
    )


def test_to_dict_is_json_serialisable_and_has_no_pixels() -> None:
    """The summary must be JSON-friendly and must NOT embed image pixels.

    Embedding base64 pixels in an observation would bloat the table; only
    metadata and positions belong there.
    """
    import json

    res = recognize_page(dark_pad_page(cards=1), dpi=SYNTH_DPI)
    payload = res.to_dict()
    text = json.dumps(payload, ensure_ascii=False)
    assert "card_count" in payload
    assert len(text) < 200_000, "summary looks like it embedded pixel data"
    assert "base64" not in text
    assert isinstance(payload["cards"], list)


def test_empty_page_does_not_raise() -> None:
    res = recognize_page(np.full((400, 300, 3), 255, np.uint8), dpi=SYNTH_DPI)
    assert res.card_count >= 0
    assert res.stage_notes


def test_single_card_image_can_skip_splitting() -> None:
    """A user-uploaded single card should not need the splitter.

    Worth asserting because the splitter's L1 fallback would produce one
    candidate anyway -- so the shortcut must be an explicit entry point,
    not an accident.
    """
    res = recognize_card(daily_card(), index=0, dpi=SYNTH_DPI)
    assert res.index == 0
    assert res.bundle is not None


@pytest.mark.parametrize("dpi", [300, 600, 1200])
def test_pipeline_runs_at_any_dpi(dpi: int) -> None:
    """N7 requires the pipeline to be DPI-agnostic.

    All thresholds are computed as mm/25.4*dpi, so changing DPI must change
    the pixel values but not whether the chain runs.
    """
    res = recognize_card(daily_card(), dpi=float(dpi))
    assert res.bundle is not None
    assert res.verdict is not None


# --------------------------------------------------------------------------- #
#  Cloud channel 2 (fallback) must never overwrite channel 1 (card JSON)
#
#  docs/ok/17 section 2: the fallback used to do `result.parsed = vlm_parsed`
#  (a whole replacement). On 003.jpg card #1 the model HAD returned
#  `inscription=西海栈道` over the JSON channel, and the fallback silently
#  dropped it -- the UI then showed "missing inscription" and asked the human
#  to retype a value the model had already read.
# --------------------------------------------------------------------------- #


def _vlm_config() -> object:
    from postmarktrip.recognition.vlm import VlmConfig

    return VlmConfig(
        provider="deepseek",
        base_url="https://example.invalid",
        model="stub",
        api_key="stub-key",
    )


def _stub_ask(replies: dict[str, str], calls: list[str]):
    """Fake `ask`: returns a canned answer per prompt, and records the calls.

    NOTE: the signature must accept ``max_tokens`` -- ``ask_card_corners``
    passes an explicit budget (the page-corner reply is much larger than a
    field reply). A stub without it fails with TypeError and looks like a
    pipeline bug.

    Matching order is **dict order**, and it is checked against the *retry*
    prompt too: `corners_prompt(expected=..., missed=...)` appends to the base
    prompt, so a needle keyed on the appended paragraph must be listed
    **before** the base-prompt needle or the retry looks like the first attempt.
    """

    def fake(
        cfg: object, image: object, prompt: str = "", *, max_tokens: int | None = None
    ) -> object:
        from postmarktrip.recognition.vlm import VlmAnswer

        calls.append(prompt)
        for needle, text in replies.items():
            if needle in prompt:
                return VlmAnswer(text=text, model="stub", provider="deepseek", latency_s=0.01)
        return VlmAnswer(
            text="", model="stub", provider="deepseek", latency_s=0.01, ok=False, reason="stub"
        )

    return fake


def test_merge_fallback_only_fills_gaps() -> None:
    """`_merge_fallback` keeps every value the first channel already read."""
    base = ParsedFields(
        inscription=Field(value="西海栈道", confidence=0.9, source_line="西海栈道"),
        place=Field(value="江西三清山", confidence=0.9, source_line="江西三清山"),
    )
    fallback = ParsedFields(
        date=Field(value="2026-10-01", confidence=0.75, source_line="2026.10.01.09"),
        place=Field(value="别的", confidence=0.75, source_line="别的"),
        date_text="2026.10.01.09",
        time_code="09",
    )
    merged, filled = _merge_fallback(base, fallback)
    assert merged.inscription is not None and merged.inscription.value == "西海栈道"
    assert merged.place is not None and merged.place.value == "江西三清山", "已有值不许被覆盖"
    assert merged.date is not None and merged.date.value == "2026-10-01", "缺的要补上"
    assert merged.time_code == "09"
    assert set(filled) == {"date", "date_text", "time_code"}


def test_fallback_is_not_even_asked_when_the_json_channel_gave_a_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**A1**: the fallback's criterion must look at the effective parse.

    It used to test the *local* OCR parse, so a card whose JSON call already
    read the date still triggered a second cloud call -- and that call
    replaced the JSON fields.
    """
    monkeypatch.setattr(
        "postmarktrip.recognition.vlm.ask",
        _stub_ask(
            {
                "JSON": '{"type":"scenic","date":"2026-10-01",'
                '"date_text":"2026.10.01.09","place":"江西三清山",'
                '"office":"","office_no":"","inscription":"西海栈道"}',
                "顶弧": "顶弧：\n中部：司春女神\n底弧：江西三清山1",
            },
            calls := [],
        ),
    )
    res = recognize_card(daily_card(), dpi=SYNTH_DPI, vlm_config=_vlm_config(), vlm_primary=True)
    assert res.field_source == "vlm_json"
    assert len(calls) == 1, f"JSON 已经给了日期，不该再跑回退：{len(calls)} 次调用"
    assert res.parsed.inscription is not None
    assert res.parsed.inscription.value == "西海栈道"


def test_fallback_fills_the_date_but_keeps_the_json_inscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**A2**: when the fallback does run, it only fills gaps."""
    monkeypatch.setattr(
        "postmarktrip.recognition.vlm.ask",
        _stub_ask(
            {
                "JSON": '{"type":"scenic","date":"","place":"","office":"",'
                '"office_no":"","inscription":"西海栈道"}',
                "顶弧": "顶弧：西海栈道\n中部：2026.10.01.09\n底弧：江西三清山1",
            },
            calls := [],
        ),
    )
    res = recognize_card(daily_card(), dpi=SYNTH_DPI, vlm_config=_vlm_config(), vlm_primary=True)
    assert res.field_source == "vlm_fallback"
    assert len(calls) == 2, "这次该跑回退（JSON 没给日期）"
    assert res.parsed.inscription is not None, "回退不许把 JSON 读到的原文冲掉"
    assert res.parsed.inscription.value == "西海栈道"
    assert res.parsed.date is not None, "日期由回退补上"
    assert any("由回退补齐的字段" in n for n in res.notes)


# --------------------------------------------------------------------------- #
#  B: cloud geometry fallback (page-level).
#
#  The trigger is deliberately narrow: the cloud is asked ONLY when the local
#  geometry looks suspicious. Two things must be pinned by tests:
#    1. a suspicious page DOES ask, and the merged geometry is used;
#    2. a clean page NEVER asks (otherwise every page costs money).
#
#  NOTE on docstrings: the file header says ASCII-only (ok/09 E-08), but the
#  B-block below uses Chinese on purpose -- it quotes the exact `stage_notes`
#  strings produced by the pipeline, and rewriting those in English would make
#  the assertions stop matching the code they are testing.
# --------------------------------------------------------------------------- #

#: Substring that identifies `PROMPT_READ_CARD_CORNERS` among all prompts.
#:
#: It must NOT be a substring of the card-JSON prompt: the stub replays the
#: same text for whichever prompt matches first, so a needle that also hits
#: `PROMPT_READ_CARD_JSON` makes the corner call receive field JSON, fail to
#: parse it, and silently look like "the cloud fallback did nothing".
#:
#: It must also survive the **retry** prompt (`corners_prompt`), which appends
#: a "还差 N 张" paragraph -- the needle is in the base prompt, so it matches
#: both attempts.
_CORNERS_NEEDLE = "平放着若干张"

#: Synthetic page for the B-block: 480x380 dark pad + up to three white cards.
#:
#: Why not reuse `dark_pad_page`: on a page this small the **bright-body**
#: pass finds nothing and the pipeline falls back to L1 (whole page as one
#: card) -- so there would be no local geometry to compare the cloud against.
#: These cards are found by the **cutline** pass instead, at exactly the pixel
#: boxes documented below, which is what the stub corner replies assume.
_CARD_BOXES = ((40, 40, 180, 300), (260, 40, 180, 300), (40, 40, 320, 100))


def b_page(*cards: int) -> np.ndarray:
    """Page with the cards at ``_CARD_BOXES[i]`` (1-based indices)."""
    page = np.full((380, 480, 3), DARK_PAD, np.uint8)
    for i in cards:
        x, y, w, h = _CARD_BOXES[i - 1]
        page[y : y + h, x : x + w] = 250
        cv2.circle(page, (x + w // 2, y + h // 2), max(8, min(w, h) // 5), (30, 30, 30), 3)
    return page


#: 云端回"一张压住本地那张 + 一张落在**右边空白**"。
#:
#: ⚠️ 两个尺寸细节，写错了会让"合并"看起来什么也没做：
#:
#: 1. 坐标是**比例**（`vlm.CORNERS_UNIT`），不是像素。写成像素的话云端候选会被
#:    整体缩到左上角一小块，然后被面积过滤掉。
#: 2. 形状必须是**真卡比例**（约 180:300 = 0.6 宽/高）。这里是 160x260 →
#:    长/短 1.63，落在 `aspect_min~aspect_max` 内；若写成 160x280 就是 1.75，
#:    会被判 `size_suspect` 并触发"形状追问"，从而多花一次调用。
#:
#: 像素依据（合成页 480x380，本地卡在 x 40~220 / x 260~440，y 40~340）：
#:   云端#1 = (60,50)-(220,310)  → 与左边本地卡 IoU **0.77** → 替换本地
#:   云端#2 = (250,50)-(400,300) → 与右边本地卡只沾 90px 宽 → IoU 0.19 → 保留两者
_CORNERS_REPLY = (
    '[{"card": 1, "corners": [[0.125,0.13158],[0.45833,0.13158],'
    "[0.45833,0.81579],[0.125,0.81579]]},"
    ' {"card": 2, "corners": [[0.52083,0.13158],[0.83333,0.13158],'
    "[0.83333,0.78947],[0.52083,0.78947]]}]"
)

#: 云端回"左边那张 + 一张本地漏掉的"：第二张落在**下方空白**，
#: 与本地任何一个候选都不重叠（轴对齐外接框不相交 → IoU = 0）。
_CORNERS_REPLY_WITH_A_MISSED_CARD = (
    '[{"card": 1, "corners": [[0.125,0.13158],[0.45833,0.13158],'
    "[0.45833,0.81579],[0.125,0.81579]]},"
    ' {"card": 2, "corners": [[0.70833,0.52632],[0.95833,0.52632],'
    "[0.95833,0.96491],[0.70833,0.96491]]}]"
)

#: 云端回"两张都与**右边**那张本地卡错开" —— 用来构造"答得够好、不该重试"。
#:
#: 像素依据（合成页 480x380；本地只放**右边**那张卡：x 260~440、y 40~340）：
#:   云端#1 = (40,40)-(220,320)   → 与本地卡只沾 40px 宽 → IoU 0.12、包含度 0.12
#:   云端#2 = (250,40)-(430,320)  → 同宽但整体左移 10px → IoU 0.88、包含度 0.94
#: 于是合计 **3 张**（2 云端 + 1 本地右卡）≥ 期望 2 → 不触发重试。
_CORNERS_REPLY_TWO_CLEAN = (
    '[{"card": 1, "corners": [[0.08333,0.10526],[0.45833,0.10526],'
    "[0.45833,0.84211],[0.08333,0.84211]]},"
    ' {"card": 2, "corners": [[0.52083,0.10526],[0.89583,0.10526],'
    "[0.89583,0.84211],[0.52083,0.84211]]}]"
)


def _corner_calls(calls: list[str]) -> list[str]:
    return [c for c in calls if _CORNERS_NEEDLE in c]


def _force_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """把几何模式钉成某一种（`local_first` = B / `cloud_first` = A）。

    ⚠️ **为什么要显式钉**：`split.GEOMETRY_MODE` 是模块级常量、默认值会随
    "B 是否达标"变（现在是 A）。如果测试依赖默认值，那么"切回 B"这个动作
    会让一批**与模式无关**的断言（去重、追问、notes 文案）一起变红 ——
    那样就分不清"回滚成功"和"回滚改坏了什么"。
    """
    from postmarktrip.recognition.split import GeometryMode

    monkeypatch.setattr("postmarktrip.recognition.pipeline.GEOMETRY_MODE", GeometryMode(mode))


def test_cloud_geometry_is_asked_when_the_page_looks_suspicious(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """可疑页（2 张 < 期望 3 张）→ 问云端，并把云端几何合进结果。

    这里是 **2 次**角点调用，而那是对的：stub 只回 2 个四边形 < 期望 3 个，
    于是"答少了"的重试分支被触发（见
    `test_cloud_geometry_retries_once_when_the_reply_is_short`）。
    要看"不重试"的形态见
    `test_cloud_geometry_does_not_retry_when_the_reply_is_complete`。
    """
    monkeypatch.setattr(
        "postmarktrip.recognition.vlm.ask",
        _stub_ask({_CORNERS_NEEDLE: _CORNERS_REPLY}, calls := []),
    )
    res = recognize_page(b_page(1, 2), dpi=SYNTH_DPI, vlm_config=_vlm_config(), expected_cards=3)
    assert len(_corner_calls(calls)) == 2, f"可疑页必须问云端（含一次重试）：{len(calls)} 次"
    assert res.split is not None
    assert res.split.geometry_source == "vlm"
    assert any("云端几何兜底：本地 2 张" in n for n in res.stage_notes)


def test_cloud_geometry_recovers_a_card_the_local_pass_missed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**B 方案的核心断言**：本地只有 1 张，云端把第 2 张补回来了。

    第二张落在下方空白处（本地什么都没检出），所以这是**真的净增一张**，
    而不是"同一张卡被两条通道各报一次"。

    这正是真实样本上的形态：`001.jpg` 底行三张被 Otsu 粘成一个 2.05×卡 的
    大连通域（矩形填充度 0.54 → 被 `min_rectangularity` 拒），云端把
    它们逐张框了出来。
    """
    monkeypatch.setattr(
        "postmarktrip.recognition.vlm.ask",
        _stub_ask({_CORNERS_NEEDLE: _CORNERS_REPLY_WITH_A_MISSED_CARD}, []),
    )
    res = recognize_page(b_page(1), dpi=SYNTH_DPI, vlm_config=_vlm_config(), expected_cards=2)
    assert res.split is not None
    assert len(res.cards) == 2, f"云端那张必须被补进来，实际 {len(res.cards)} 张"
    #: 逐卡记录来源：这是界面要标的那个值（页级只说"这页用过云端"）
    assert [c.geometry_source for c in res.cards].count("vlm") >= 1
    assert any("云端几何兜底：本地 1 张" in n for n in res.stage_notes)


def test_local_first_mode_does_not_ask_the_cloud_when_the_page_is_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**B 模式（`local_first`）下不该触发时一次都不调** —— 否则每页都多花钱。

    ⚠️ 这条必须先 monkeypatch 回 `LOCAL_FIRST`：`ok/20` §七 授权"B 不达标就上 A"，
    而 A（`cloud_first`）**每页都问**，所以在 A 模式下跑这条断言必然失败 ——
    那不是 bug，是模式不同。A 的行为由下一条测试钉住。
    """
    _force_mode(monkeypatch, "local_first")
    monkeypatch.setattr(
        "postmarktrip.recognition.vlm.ask",
        _stub_ask({_CORNERS_NEEDLE: _CORNERS_REPLY}, calls := []),
    )
    res = recognize_page(
        b_page(1, 2),
        dpi=SYNTH_DPI,
        vlm_config=_vlm_config(),
        #: 这一页就该有 2 张卡 → 数量、形状、面积全都正常 → 不该调云端
        expected_cards=2,
    )
    assert _corner_calls(calls) == [], f"干净的页不该调云端：{_corner_calls(calls)}"
    assert res.split is not None
    assert res.split.geometry_source == "local"
    assert any("正常（不调云端）" in n for n in res.stage_notes)


def test_cloud_first_mode_asks_the_cloud_even_on_a_clean_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**A 模式（`cloud_first`）每页都问** —— 这正是 A 的定义与它的代价。

    "干净的页"在 A 里没有特殊待遇：几何以云端为准，所以必须问。
    代价按页增加 1~2 次调用（追问是条件性的），用户 2026-10-13 已知情接受。
    """
    _force_mode(monkeypatch, "cloud_first")
    monkeypatch.setattr(
        "postmarktrip.recognition.vlm.ask",
        _stub_ask({_CORNERS_NEEDLE: _CORNERS_REPLY}, calls := []),
    )
    res = recognize_page(b_page(1, 2), dpi=SYNTH_DPI, vlm_config=_vlm_config(), expected_cards=2)
    assert len(_corner_calls(calls)) >= 1, "A 模式下每页都该问云端"
    assert res.split is not None
    assert res.split.geometry_source == "vlm"
    assert any("云端优先（每页都问）" in n for n in res.stage_notes)
    assert any("交叉校验" in n for n in res.stage_notes), "A 要求本地留作交叉校验"


def test_cloud_geometry_is_not_asked_without_a_vlm_config() -> None:
    """**默认（`use_vlm=false`）零云端调用** —— 这条保证 `_diag_split.py`
    那种"只看几何"的诊断可以反复跑，不花钱也不受云端影响。

    ⚠️ 这条与 `ALWAYS` 不冲突：**A 模式也要 `use_vlm=true` 才生效**。
    开关的层级是：`use_vlm`（要不要用云端）> `GEOMETRY_MODE`（用哪种几何策略）。
    A 改的是"什么时候问"，不是"不问也能拿到云端结果"。
    """
    res = recognize_page(b_page(1), dpi=SYNTH_DPI, expected_cards=9)
    assert res.split is not None
    assert res.split.geometry_source == "local"
    assert not any("几何可疑性检查" in n for n in res.stage_notes), "没开云端就不该做这个检查"
    assert not any("几何模式" in n for n in res.stage_notes)


def test_cloud_geometry_retries_once_when_the_reply_is_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """云端**答少了**会带张数线索再问一次，并把两次的并集用起来。

    实测（2026-10-13）`001.jpg` 同图同提示两次分别回了 9 条与 8 条 ——
    模型自己会漏一张。所以"少一张"必须有个应对，而应对**不能是编一张出来**：
    第二次只是把缺口说出来（"还差 1 张，再看看边缘"），它仍只能给真存在的卡。

    ⚠️ 这一页必须**可疑**（2 张 < 期望 3 张）才会走到云端 —— 而 stub 回的是
    2 个四边形，于是"回少了"这个分支被真正触发。所以这里钉 `local_first`（B）：
    在 A 下"可疑"这个概念不参与判断，测的就不是这条分支了。
    """
    _force_mode(monkeypatch, "local_first")
    replies = {
        #: 第二次带提示（提示里出现"还差"）：把两张都给出来。
        #: ⚠️ 必须排在 base needle **前面** —— `_stub_ask` 按 dict 顺序匹配，
        #: 而重试提示**同时**含有 base needle。
        "还差": _CORNERS_REPLY,
        #: 第一次：只有 1 张（少于 expected=3）
        _CORNERS_NEEDLE: _CORNERS_REPLY.split("},")[0] + "}]",
    }
    monkeypatch.setattr("postmarktrip.recognition.vlm.ask", _stub_ask(replies, calls := []))
    res = recognize_page(b_page(1, 2), dpi=SYNTH_DPI, vlm_config=_vlm_config(), expected_cards=3)
    #: 第 1 次（原提示）+ 第 2 次（带提示）= 2 次角点调用
    assert len(_corner_calls(calls)) == 2, f"该重试一次，实际 {len(_corner_calls(calls))} 次"
    assert res.split is not None
    assert any("云端几何兜底" in n for n in res.stage_notes)


def test_cloud_geometry_does_not_retry_when_the_reply_is_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """答够了、形状也对 → **只花一次调用**（重试是纠正"答不对"，不是常态）。

    这一页放本地卡，`expected_cards=2` → **可疑**（钉 `local_first` 才有这个概念），
    而 stub 回的 2 个四边形里，落到本地卡上的那个**包含度 0.94** → 判为同一张、
    被替换。合计 2 张 ≥ 期望 2、且没有 `size_suspect` → 不满足任何重试条件。
    """
    _force_mode(monkeypatch, "local_first")
    monkeypatch.setattr(
        "postmarktrip.recognition.vlm.ask",
        _stub_ask({_CORNERS_NEEDLE: _CORNERS_REPLY_TWO_CLEAN}, calls := []),
    )
    res = recognize_page(b_page(2), dpi=SYNTH_DPI, vlm_config=_vlm_config(), expected_cards=2)
    assert len(_corner_calls(calls)) == 1, f"答够了不该重试：{len(_corner_calls(calls))} 次"
    assert res.split is not None
    assert len(res.cards) == 2
    assert all(c.geometry_source == "vlm" for c in res.cards)


def test_retry_does_not_accumulate_both_cloud_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**回归**：追问的第二份答案必须从**本地**重新合并，不能叠加在第一份上。

    实测现场（2026-10-13，`005.jpg`）：第一份云端答案把底行三张框歪了
    （350x209、宽高比 1.68），而本地 7 张里只有 3 张与它重叠 —— 剩下 4 张本地卡
    留在结果里；此时再叠上第二份答案（两份云端彼此不重叠），最后 **13 张**
    （正确 9）。那是"越修越坏"，而且从数字上看不出来。

    这里用最小的合成页复现同一个形状：本地只检到 1 张（期望 3 → 可疑）。
    第 1 份答案给 **2 张**（一张框住本地那张 + 一张落在右上的**垃圾框**），
    第 2 份给 **1 张**（只有框住本地那张的那一个）。正确做法 = 两份**各自**
    与本地合并后取优 → **1 张**；叠加的写法会得到 2 张（垃圾框活下来）。
    """
    _force_mode(monkeypatch, "local_first")
    one_bad = (
        '[{"card": 1, "corners": [[0.125,0.13158],[0.45833,0.13158],'
        "[0.45833,0.81579],[0.125,0.81579]]},"
        ' {"card": 2, "corners": [[0.72917,0.13158],[1.0625,0.13158],'
        "[1.0625,0.81579],[0.72917,0.81579]]}]"
    )
    one_good = (
        '[{"card": 1, "corners": [[0.125,0.13158],[0.45833,0.13158],'
        "[0.45833,0.81579],[0.125,0.81579]]}]"
    )
    #: 追问提示里带"竖版矩形卡"那段（`CORNERS_SHAPE_HINT` 的原话）；第一次只有
    #: base needle。⚠️ 键**必须逐字取自 hint 文本**，而且不能出现在 base 提示里 ——
    #: 否则两次都命中同一份答案，这条测试就退化成"两次答案一样"，测不出"叠加"。
    replies = {"竖版矩形卡": one_good, _CORNERS_NEEDLE: one_bad}
    monkeypatch.setattr("postmarktrip.recognition.vlm.ask", _stub_ask(replies, calls := []))
    res = recognize_page(b_page(1), dpi=SYNTH_DPI, vlm_config=_vlm_config(), expected_cards=3)
    assert len(_corner_calls(calls)) == 2, "该追问一次"
    assert res.split is not None
    #: 第 2 份（1 张）胜出；**绝不能**把第 1 份那张垃圾框也留下
    assert len(res.cards) == 1, f"两份云端答案不许叠加，实际 {len(res.cards)} 张"
    assert any("两次云端答案取优" in n for n in res.stage_notes)
    assert any("未采用的云端答案" in n for n in res.stage_notes), "被放弃那份的原因要留证"


def test_cloud_geometry_failure_keeps_the_local_geometry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """云端调用**失败**（或回空）→ 本地几何一字不改，且不静默。

    两次调用都会失败（stub 什么都不匹配），重试也救不回来 —— 这仍然是对的：
    追问只在"答得不对"时发生，而"根本没答上来"追问一次是合理的补救。
    """
    monkeypatch.setattr(
        "postmarktrip.recognition.vlm.ask",
        _stub_ask({}, []),  # 什么都不匹配 → ok=False
    )
    res = recognize_page(b_page(1, 2), dpi=SYNTH_DPI, vlm_config=_vlm_config(), expected_cards=3)
    assert res.split is not None
    assert res.split.geometry_source == "local", "调用失败时几何仍然是本地的"
    assert len(res.cards) == 2
    assert all(c.geometry_source == "local" for c in res.cards)
    assert any("云端角点（hint=无）：ok=False" in n for n in res.stage_notes), res.stage_notes
