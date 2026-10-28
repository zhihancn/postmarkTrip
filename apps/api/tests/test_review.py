"""Confidence model and review queue tests (spec 3.7).

The central assertion here is the one that was learned by measurement:
confidence CANNOT identify the OCR confusions we actually see, because they
are 1-2 characters off and come with HIGH confidence. So there are two
independent defences, and both are tested.

NOTE: ASCII-only docstrings -- see docs/ok/09-P2-non-code-errors.md E-08.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from postmarktrip.recognition.fields import Field, ParsedFields
from postmarktrip.recognition.review import (
    FIELD_THRESHOLDS,
    PIPELINE_VERSION,
    build_bundle,
    decide,
    observation_decimals,
    reclassify_for_postmark_type,
)

# --------------------------------------------------------------------------- #
#  Per-field thresholds
# --------------------------------------------------------------------------- #


def test_date_can_auto_accept_at_a_low_confidence() -> None:
    """Date is mechanically validated (regex + month/day ranges), so it does
    not need a high confidence to be trustworthy.

    Requiring 0.9 here would send every correct date to human review and
    waste the reviewer's time on the one field that is already reliable.
    """
    decision, _ = decide("date", 0.60, value="2026-09-25")
    assert decision == "auto_accept"


def test_free_text_fields_need_much_higher_confidence() -> None:
    """Place/office names are free text: no mechanical check can detect that
    "福建南火" is wrong, so the bar must be higher than for a date."""
    _, date_upper = FIELD_THRESHOLDS["date"]
    _, place_upper = FIELD_THRESHOLDS["place"]
    assert place_upper > date_upper, (
        "a free-text field must require more confidence than a validated one"
    )
    decision, _ = decide("place", 0.88, value="福建南平")
    assert decision == "needs_review"


def test_below_lower_bound_is_rejected() -> None:
    """Below the lower bound we treat the field as unread rather than as a
    doubtful value -- a value we are nearly sure is wrong is worse than none."""
    decision, reason = decide("place", 0.10, value="福")
    assert decision == "reject"
    assert "下限" in reason


def test_empty_value_is_always_rejected() -> None:
    assert decide("date", 1.0, value="")[0] == "reject"
    assert decide("place", 1.0, value="")[0] == "reject"


# --------------------------------------------------------------------------- #
#  The second defence: known-value membership
# --------------------------------------------------------------------------- #


def test_high_confidence_unknown_place_still_goes_to_review() -> None:
    """**This is the lesson from measurement.**

    Measured OCR confusions on the real samples are all 1-2 characters and
    carry HIGH confidence:

        福建南火 (0.90)  vs truth 福建南平
        西玉山   (0.72)  vs truth 江西玉山
        乔山     (0.71)  vs truth 茶山

    With confidence alone, "福建南火" at 0.90 would be auto-accepted. So an
    unregistered value must go to review REGARDLESS of confidence.
    """
    decision, reason = decide(
        "place", 0.90, value="福建南火", known_values=frozenset({"福建南平", "福建厦门"})
    )
    assert decision == "needs_review", "a confident wrong value would be auto-accepted"
    assert "词表" in reason


def test_high_confidence_known_place_may_auto_accept() -> None:
    """The other side of the same rule: a registered value with high
    confidence is the only case that auto-accepts."""
    decision, _ = decide("place", 0.95, value="福建南平", known_values=frozenset({"福建南平"}))
    assert decision == "auto_accept"


def test_unknown_place_goes_to_review_even_at_max_confidence() -> None:
    """Even 1.00 is not enough for an unregistered value.

    Deliberately strict: a wrong place name silently accepted is worse than
    one extra review click, because the wrong value can propagate into the
    gallery and exports.
    """
    decision, _ = decide("place", 1.0, value="某不存在的地名", known_values=frozenset({"福建南平"}))
    assert decision == "needs_review"


def test_date_ignores_known_values() -> None:
    """Dates are not looked up in a vocabulary -- passing known_values for a
    date must not change its decision."""
    a, _ = decide("date", 0.60, value="2026-09-25")
    b, _ = decide("date", 0.60, value="2026-09-25", known_values=frozenset({"2020-01-01"}))
    assert a == b == "auto_accept"


# --------------------------------------------------------------------------- #
#  Bundle construction
# --------------------------------------------------------------------------- #


def _parsed(**kw: object) -> ParsedFields:
    p = ParsedFields()
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def test_bundle_records_every_field_including_misses() -> None:
    """The L2 observation layer records what the machine saw -- including
    what it did NOT see.

    Keeping missed fields lets us later analyse "which fields are often
    unreadable", which is impossible if only successes are stored.
    """
    parsed = _parsed(date=Field(value="2026-09-25", confidence=0.96, source_line="2026.09.25.15"))
    bundle = build_bundle(parsed, postmark_type="daily", postmark_type_confidence=0.69)
    names = {o.field_name for o in bundle.observations}
    assert {"date", "place", "office"} <= names
    assert all(o.field_name for o in bundle.observations)

    place = next(o for o in bundle.observations if o.field_name == "place")
    assert place.decision == "reject"
    assert "未抽到" in place.reason


def test_every_observation_carries_the_pipeline_version() -> None:
    """Required so that a model upgrade can re-run history and compare.

    Without it, observations from different pipelines are indistinguishable
    and "re-run and pick the better result" is impossible.
    """
    bundle = build_bundle(_parsed(), postmark_type="daily", postmark_type_confidence=0.7)
    assert bundle.observations, "no observations built"
    for o in bundle.observations:
        assert o.pipeline_version == PIPELINE_VERSION
    for row in observation_decimals(bundle):
        assert row["pipeline_version"] == PIPELINE_VERSION


def test_queue_is_sorted_least_confident_first() -> None:
    """Reviewer time should go to the most doubtful field first."""
    parsed = _parsed(
        date=Field(value="2026-09-25", confidence=0.45, source_line="2026.09.25.15"),
        place=Field(value="福建南火", confidence=0.90, source_line="福建南火"),
        office=Field(value="建溪", confidence=0.60, source_line="建溪"),
    )
    bundle = build_bundle(parsed)
    confidences = [it.confidence for it in bundle.queue]
    assert confidences == sorted(confidences), f"queue not sorted: {confidences}"


def test_confident_wrong_place_lands_in_the_queue() -> None:
    """End-to-end version of the key lesson: the near-miss must surface."""
    parsed = _parsed(place=Field(value="福建南火", confidence=0.90, source_line="福建南火"))
    bundle = build_bundle(parsed, known_places=frozenset({"福建南平"}))
    assert any(it.field_name == "place" and it.value == "福建南火" for it in bundle.queue), (
        "the 1-character OCR error did not reach the review queue"
    )


def test_no_queue_means_everything_auto_accepted() -> None:
    parsed = _parsed(date=Field(value="2026-09-25", confidence=0.99, source_line="2026.09.25.15"))
    bundle = build_bundle(parsed)
    assert bundle.queue == []
    assert any("队列为空" in n for n in bundle.notes)


def test_bbox_is_propagated_to_observations() -> None:
    """The review UI needs to draw where the value came from."""
    parsed = _parsed(date=Field(value="2026-09-25", confidence=0.99, source_line="x"))
    bundle = build_bundle(parsed, bbox=(10, 20, 30, 40))
    for o in bundle.observations:
        assert o.bbox == {"x": 10, "y": 20, "w": 30, "h": 40}


# --------------------------------------------------------------------------- #
#  戳面原文（`docs/ok/16` §四）
# --------------------------------------------------------------------------- #


def _inscription(bundle: object) -> object:
    return next(o for o in bundle.observations if o.field_name == "inscription")  # type: ignore[attr-defined]


def test_inscription_is_always_recorded_even_when_missing() -> None:
    """它**必填**，所以"没读到"这件事必须出现在观察档案里（否则界面不知道该拦）。"""
    bundle = build_bundle(_parsed(), postmark_type="scenic")
    row = _inscription(bundle)
    assert row.decision == "reject"  # type: ignore[attr-defined]
    assert "必填" in row.reason  # type: ignore[attr-defined]


def test_scenic_stamp_never_has_its_inscription_fabricated() -> None:
    """风景戳的原文在**图画里**，拼不出来 —— 宁可为空让人填（`ok/16` §4.3）。

    这条是防"顺手用局名+戳号兜底"：那会把一个**错的原文**写进证据列。
    """
    parsed = _parsed(
        office=Field(value="三清山", confidence=0.9, source_line="三清山"),
        office_no="1",
    )
    bundle = build_bundle(parsed, postmark_type="scenic")
    row = _inscription(bundle)
    assert row.normalized_value == ""  # type: ignore[attr-defined]
    assert row.decision == "reject"  # type: ignore[attr-defined]


def test_daily_stamp_inscription_may_be_built_from_office_plus_number() -> None:
    """日戳的原文**按用户给的定义**就是「局名+戳号」——那时那串字是确定的。

    但 reason 里必须写明**这是拼的**（不是模型读到的原文），人仍可改。
    """
    parsed = _parsed(
        office=Field(value="建溪", confidence=0.9, source_line="建溪 1"),
        office_no="1",
    )
    bundle = build_bundle(parsed, postmark_type="daily")
    row = _inscription(bundle)
    assert row.normalized_value == "建溪1"  # type: ignore[attr-defined]
    assert "拼出" in row.reason  # type: ignore[attr-defined]


def test_read_inscription_wins_over_the_built_one() -> None:
    """模型真的读到原文时**不拼** —— 原文优先，拼只是兜底。"""
    parsed = _parsed(
        inscription=Field(value="三清山三龙出海", confidence=0.9, source_line="三清山三龙出海"),
        office=Field(value="三清山", confidence=0.9, source_line="三清山"),
        office_no="1",
    )
    bundle = build_bundle(parsed, postmark_type="daily")
    row = _inscription(bundle)
    assert row.normalized_value == "三清山三龙出海"  # type: ignore[attr-defined]


def test_date_text_and_time_code_become_observations() -> None:
    """两列（`postmark_date_text` / `time_code`）此前从没有人填 —— 现在要有。"""
    parsed = _parsed(
        date=Field(value="2026-09-25 15:00", confidence=0.97, source_line="2026.09.25.15"),
        date_text="2026.09.25.15",
        time_code="15",
    )
    bundle = build_bundle(parsed, postmark_type="daily")
    rows = {o.field_name: o.normalized_value for o in bundle.observations}
    assert rows["date_text"] == "2026.09.25.15"
    assert rows["time_code"] == "15"


# --------------------------------------------------------------------------- #
#  Persistence shape
# --------------------------------------------------------------------------- #


def test_confidence_is_decimal_not_float() -> None:
    """``Observation.confidence`` is ``Numeric(5,4)``.

    Writing a float through some drivers silently truncates to 0, and a
    column of zeros just looks like "poor recognition quality" rather than
    like a bug. Hence Decimal.
    """
    parsed = _parsed(date=Field(value="2026-09-25", confidence=0.9643, source_line="x"))
    bundle = build_bundle(parsed)
    rows = observation_decimals(bundle)
    for row in rows:
        assert isinstance(row["confidence"], Decimal), (
            f"confidence is {type(row['confidence'])}, expected Decimal"
        )


def test_confidence_is_rounded_to_four_places() -> None:
    """Numeric(5,4) holds 4 decimal places; rounding here avoids driver-side
    rounding differences between environments."""
    parsed = _parsed(date=Field(value="2026-09-25", confidence=0.123456789, source_line="x"))
    rows = observation_decimals(build_bundle(parsed))
    date_row = next(r for r in rows if r["field_name"] == "date")
    assert date_row["confidence"] == Decimal("0.1235")


def test_empty_strings_become_none_in_rows() -> None:
    """Empty raw/normalized values become NULL rather than "".

    An empty string and "not read" are different things in SQL, and only
    NULL lets "how often was this field unreadable" be queried directly.
    """
    rows = observation_decimals(build_bundle(_parsed()))
    for row in rows:
        assert row["raw_value"] is None
        assert row["normalized_value"] is None


@pytest.mark.parametrize("field", ["date", "place", "office", "office_no"])
def test_all_expected_fields_have_thresholds(field: str) -> None:
    assert field in FIELD_THRESHOLDS
    lower, upper = FIELD_THRESHOLDS[field]
    assert 0.0 <= lower < upper <= 1.0, f"{field}: bad thresholds {(lower, upper)}"


# --------------------------------------------------------------------------- #
#  按戳型分流：风景戳的"顶弧文字"要改判为局名
#
#  用户 2026-10-08 定的规则。判据来自项目自己（tools/samples/a3_vlm_whole.py）：
#    日戳   顶弧（上半圆）有省市地名文字，中部日期，底弧局名+戳号
#    风景戳 顶弧是空的，中部或侧面是图案/线描/书法名称
#
#  所以"这串字算不算地名"取决于戳型。这组测试盯的是**三道保护**，
#  因为缺任何一个都会把本来对的值改错 —— 而那是静默的。
# --------------------------------------------------------------------------- #


class _VocabStub:
    """最小词表查询器：只要 `lookup(value).known` 与 `.place_path`。"""

    def __init__(self, known: set[str]) -> None:
        self._known = known

    def lookup(self, value: str) -> object:
        known = value in self._known
        path = (value,) if known else ()
        return type("_R", (), {"known": known, "place_path": path, "hit": None})()


def _reclassify(
    parsed: ParsedFields,
    kind: str,
    *,
    places: set[str] | None = None,
    offices: set[str] | None = None,
) -> str:
    return reclassify_for_postmark_type(
        parsed,
        postmark_type=kind,
        place_vocab=_VocabStub(places or set()),
        known_places=None,
        office_vocab=_VocabStub(offices or set()),
        known_offices=None,
    )


def test_scenic_top_arc_text_becomes_office() -> None:
    """风景戳：顶弧/中部那串字**在局名词表里命中** → 改判为局名，`place` 留空。

    "戳面没有任何地名信息的话就留空" —— 不编一个省出来。
    ⚠️ 注意 `offices={"三清山"}` 这个前提：按 `ok/17` §三 B0 的新判据，
    **只有命中局名词表才搬** —— `三清山` 正是用户 2026-10-08 举的那个例子
    （它既是景点名、又是三清山邮政所的短名）。
    """
    parsed = _parsed(place=Field(value="三清山", confidence=0.93, source_line="三清山"))
    note = _reclassify(parsed, "scenic", offices={"三清山"})
    assert note, "应当有说明"
    assert parsed.office is not None
    assert parsed.office.value == "三清山"
    assert parsed.place is None, "改判后 place 必须留空（不编）"


def test_scenic_moves_when_only_the_new_candidate_is_known() -> None:
    """保护 ③ 的**该换**那一半：新候选在局名词表里、原候选不在 → 换。

    这时新候选（来自风景戳的名称行）比一个不在词表里的旧值可信。
    """
    parsed = _parsed(
        place=Field(value="三清山", confidence=0.94, source_line="三清山"),
        office=Field(value="江西三清山", confidence=0.80, source_line="江西三清山"),
    )
    note = _reclassify(parsed, "scenic", offices={"三清山"})
    assert note and "改判为**局名**" in note
    assert parsed.office is not None and parsed.office.value == "三清山"
    assert parsed.place is None


def test_scenic_keeps_a_real_place_name_in_place() -> None:
    """**保护 ②**：值本身是已登记地名就不搬。

    实测真值：`单张卡整张扫描图.jpg` 是**风景戳**，但 `place` = `江西玉山`、
    `office` = `三清乡` —— **两个字段都对**。若不设这道保护，`江西玉山`
    会被错误地搬进 office。
    """
    parsed = _parsed(
        place=Field(value="江西玉山", confidence=0.95, source_line="江西玉山"),
        office=Field(value="三清乡", confidence=0.90, source_line="三清乡 1"),
    )
    note = _reclassify(parsed, "scenic", places={"江西玉山", "福建省"})
    assert note == "", "已登记地名不该被搬动"
    assert parsed.place is not None and parsed.place.value == "江西玉山"
    assert parsed.office is not None and parsed.office.value == "三清乡"


def test_daily_top_arc_text_is_never_touched() -> None:
    """**保护 ①**：日戳的顶弧就是地名，绝不能碰。"""
    parsed = _parsed(place=Field(value="福建南火", confidence=0.90, source_line="福建南火"))
    note = _reclassify(parsed, "daily")
    assert note == ""
    assert parsed.place is not None and parsed.place.value == "福建南火"
    assert parsed.office is None


def test_scenic_keeps_the_office_that_is_in_the_office_vocab() -> None:
    """**保护 ③**：office 已有"在局名词表里"的值时，保留它，只写说明。

    宁可少搬，也不要静默丢掉一个已经读到的局名。
    """
    parsed = _parsed(
        place=Field(value="司春女神", confidence=0.91, source_line="司春女神"),
        office=Field(value="三清乡", confidence=0.88, source_line="三清乡 1"),
    )
    note = _reclassify(parsed, "scenic", offices={"三清乡"})
    #: `司春女神` 不在局名词表里 → 保护④先把它挡掉（"两个字段都不进"）
    assert "不进地名、也不进局名" in note
    assert parsed.office is not None and parsed.office.value == "三清乡"
    assert parsed.place is None


def test_scenic_keeps_the_existing_office_when_both_are_known() -> None:
    """两个候选**都**是已登记局名时，保留原 office。

    依据是**结构**，不是"哪个更可信"：
      * `office` 来自**底弧** —— 项目自己的判据说那里**就是**放局名的位置
      * `place` 在风景戳上来自**中部名称行** —— 那里可以是**图案名**

    所以判据是**非对称**的（新候选可信 **且** 原候选不可信才换）。
    我一度改成"只要求新候选可信"，被这条测试抓住 —— 那个理由是错的。
    """
    parsed = _parsed(
        place=Field(value="三清山", confidence=0.94, source_line="三清山"),
        office=Field(value="建溪", confidence=0.80, source_line="建溪 1"),
    )
    note = _reclassify(parsed, "scenic", offices={"三清山", "建溪"})
    assert "未搬进局名" in note
    assert parsed.office is not None and parsed.office.value == "建溪"
    #: ⚠️ 新规则（`ok/17` §三 B0）：既然判定"它不是地名"，**也不该留在地名里**
    assert parsed.place is None


def test_scenic_scenic_name_neither_place_nor_office() -> None:
    """**保护 ④（新，2026-10-11）**：不在局名词表里的那串字**两个字段都不进**。

    用户原话："西海栈道怎么能是'地名'呢？我宁可这个地名是空的"。
    这条规则最初是照他 2026-10-08 的澄清记的，而当时举的例子是 `三清山` ——
    那串字恰好**既是景点名、又是三清山邮政所的短名**，于是"图案文字 → 局名"
    被写成了**无条件**的规则，把纯景点名也搬了进去。
    """
    parsed = _parsed(place=Field(value="西海栈道", confidence=0.90, source_line="西海栈道"))
    note = _reclassify(parsed, "scenic", offices={"三清山", "三清乡"})
    assert "不进地名、也不进局名" in note
    assert parsed.place is None
    assert parsed.office is None


def test_scenic_scenic_name_is_dropped_even_when_office_is_free() -> None:
    """office 空着也不搬 —— 这正是旧规则的错处（`ok/17` §三）。"""
    parsed = _parsed(place=Field(value="司春女神", confidence=0.90, source_line="司春女神"))
    _reclassify(parsed, "scenic", offices={"三清山"})
    assert parsed.office is None, "纯景点名不该变成局名"
    assert parsed.place is None, "也不该留在地名里"


def test_bundle_explains_where_the_dropped_place_went() -> None:
    """`place` 那条 reject 的 reason 要**说清那串字去哪了**。

    通用那句"未抽到（OCR 未返回匹配的行）"在这里是误导 —— OCR 明明读到了。
    """
    parsed = _parsed(place=Field(value="西海栈道", confidence=0.93, source_line="西海栈道"))
    bundle = build_bundle(
        parsed,
        postmark_type="scenic",
        office_vocab=_VocabStub({"三清山"}),
    )
    place = next(o for o in bundle.observations if o.field_name == "place")
    assert "不进地名、也不进局名" in place.reason
    assert "未抽到" not in place.reason
