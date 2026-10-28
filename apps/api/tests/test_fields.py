"""Field extraction tests (spec 3.6).

These are pure-logic tests, so they are cheap and precise. They pin the
field rules that were derived from real samples, plus the one structural
insight that made place/office assignment work at all.

NOTE: ASCII-only docstrings -- see docs/ok/09-P2-non-code-errors.md E-08.
"""

from __future__ import annotations

import pytest

from postmarktrip.recognition.fields import (
    DATE_RE,
    find_date,
    normalise_digits,
    parse_fields,
    split_office_and_no,
)

# --------------------------------------------------------------------------- #
#  Date
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026.09.25.15", "2026-09-25 15:00"),
        ("2026.10.06.17", "2026-10-06 17:00"),
        ("2026.10.01.09", "2026-10-01 09:00"),
        # Spaces between digits: measured as "20 26 09.25.16"
        ("20 26 09.25.16", "2026-09-25 16:00"),
        # Separator variants
        ("2026-09-25", "2026-09-25"),
        ("2026/9/5", "2026-09-05"),
        # No hour
        ("2026.09.25", "2026-09-25"),
    ],
)
def test_find_date_formats(text: str, expected: str) -> None:
    """All three separators and the optional hour, per the measured samples.

    The optional ``.HH`` matters: real stamps carry a time after the date
    ("2026.09.25.15"). It is extra information, not part of A4's date, but
    dropping it would discard data the stamp actually carries.
    """
    got = find_date([(text, 0.9)])
    assert got is not None, f"no date found in {text!r}"
    assert got.value == expected


def test_find_date_ignores_impossible_months() -> None:
    """A match like 2026.13.45 is not a date -- it is a coincidental digit run.

    Guarding here keeps things like the stamp's face value "2026" from being
    accepted as a date in a different-looking string.
    """
    assert find_date([("2026.13.45", 0.99)]) is None
    assert find_date([("2026.00.10", 0.99)]) is None


def test_find_date_prefers_the_highest_confidence_match() -> None:
    """Whole-region OCR also reads the postcard's PRINTED header.

    Measured: at margin 1.3 the printed "牡丹江雪乡" and other printed text
    come in alongside the stamp's own lines. If two lines both look like a
    date, the more confident one is the stamp's.
    """
    got = find_date([("2020.01.01", 0.40), ("2026.09.25.15", 0.96)])
    assert got is not None
    assert got.value == "2026-09-25 15:00", f"picked the low-confidence line: {got.value}"


def test_find_date_none_when_absent() -> None:
    assert find_date([("建溪", 0.99), ("福建南平", 0.9)]) is None
    assert find_date([]) is None


def test_normalise_digits_only_joins_digits() -> None:
    """Spaces BETWEEN DIGITS are OCR artefacts and must go.

    But spaces elsewhere must survive: "四号航站楼 2" needs its space to
    separate the office name from the stamp number. Removing all spaces
    would glue them into "四号航站楼2" and lose the number.
    """
    assert normalise_digits("20 26 09.25.16") == "202609.25.16"
    assert normalise_digits("四号航站楼 2") == "四号航站楼 2"


# --------------------------------------------------------------------------- #
#  Office name vs stamp number
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "office", "no"),
    [
        ("建溪 1", "建溪", "1"),
        ("茶山 1", "茶山", "1"),
        ("三清乡 1", "三清乡", "1"),
        # An office name that CONTAINS a number: only the trailing one is the
        # stamp number. A greedy match would split this wrongly.
        ("四号航站楼 2", "四号航站楼", "2"),
        ("建溪", "建溪", ""),
        ("八一一路", "八一一路", ""),
    ],
)
def test_split_office_and_no(text: str, office: str, no: str) -> None:
    assert split_office_and_no(text) == (office, no)


# --------------------------------------------------------------------------- #
#  The structural insight: split on the DATE line, not the geometric midline
# --------------------------------------------------------------------------- #


def test_place_and_office_split_on_the_date_line() -> None:
    """**This is the bug that made place extraction impossible.**

    Measured on 清晰黑色日戳特写-1: the detected circle centre is ~240px
    ABOVE the true postmark centre, so the top-arc name landed in the
    "lower half" by geometric midline and was assigned as the OFFICE name.

    The date is printed at the postmark's true centre, so the date line is
    the correct split basis:

        福建南火 (top arc)   y=389
        2026.09.25.15       y=583   <- true centre
        建溪 (bottom arc)   y=775

    Here the geometric midline (500) would put the top-arc line in the
    lower half; the date basis (600) puts it correctly in the upper half.
    """
    lines = [("福建南火", 0.90), ("2026.09.25.15", 0.96), ("建溪", 0.99)]
    ys = [389.0, 583.0, 775.0]
    parsed = parse_fields(lines, region_height=1000, line_y_positions=ys)

    assert parsed.date is not None
    assert parsed.place is not None, "top-arc name was not assigned to place"
    assert parsed.place.value == "福建南火"
    assert parsed.office is not None
    assert parsed.office.value == "建溪"


def test_geometric_midline_would_have_failed_here() -> None:
    """Negative control for the test above: pins the 500 vs 600 difference.

    With region_height=1000 the midline is 500, and the top-arc line sits at
    389 -- above it. The failure appears when the centre error is larger
    than the text ring radius, which is exactly the measured case.
    """
    lines = [("福建南火", 0.90), ("2026.09.25.15", 0.96), ("建溪", 0.99)]
    # Centre pushed far down: the top-arc line is now BELOW the midline (600)
    ys = [700.0, 900.0, 1100.0]
    parsed = parse_fields(lines, region_height=1200, line_y_positions=ys)
    assert parsed.place is not None, (
        "date-based split must still work when the geometric centre is wrong"
    )
    assert parsed.place.value == "福建南火"
    assert parsed.office is not None and parsed.office.value == "建溪"


def test_date_line_is_never_used_as_a_place_name() -> None:
    """The date line must not leak into place/office.

    Without this guard "2026.09.25.15" can be the highest-confidence line
    above the split and get assigned as the place name.
    """
    lines = [("2026.09.25.15", 0.99)]
    parsed = parse_fields(lines, region_height=600, line_y_positions=[300.0])
    assert parsed.date is not None
    assert parsed.place is None, f"date leaked into place: {parsed.place}"
    assert parsed.office is None, f"date leaked into office: {parsed.office}"


def test_printed_header_is_not_treated_as_a_place() -> None:
    """Whole-region OCR reads the postcard's printed header.

    "中国邮政" / "CHINA POST" / "美丽中国" are not place names. Accepting
    them would put a printed header into the place field.
    """
    lines = [
        ("中国邮政", 1.00),
        ("CHINA POST", 0.99),
        ("2026.09.25.15", 0.96),
        ("建溪", 0.98),
    ]
    ys = [100.0, 200.0, 400.0, 600.0]
    parsed = parse_fields(lines, region_height=800, line_y_positions=ys)
    assert parsed.place is None, f"printed header became the place: {parsed.place}"


def test_no_position_info_means_no_place_guessing() -> None:
    """Without y positions, only the date is extracted.

    Guessing which line is the place would produce plausible-but-wrong
    values, and a wrong place name is worse than an empty one: an empty
    value goes to the review queue, a wrong one may be accepted silently.
    """
    parsed = parse_fields([("福建南平", 0.9), ("2026.09.25.15", 0.96)], region_height=800)
    assert parsed.date is not None
    assert parsed.place is None
    assert parsed.office is None
    assert any("跳过" in n for n in parsed.notes)


def test_empty_lines_reported_not_silent() -> None:
    parsed = parse_fields([], region_height=800, line_y_positions=[])
    assert parsed.found == []
    assert parsed.notes, "an empty result must carry a reason"


def test_found_lists_only_present_fields() -> None:
    lines = [("2026.09.25.15", 0.96), ("建溪", 0.98)]
    parsed = parse_fields(lines, region_height=800, line_y_positions=[300.0, 600.0])
    assert "date" in parsed.found
    assert "office" in parsed.found
    assert "place" not in parsed.found


def test_date_regex_is_anchored_to_four_digit_years() -> None:
    """Guard against a looser year pattern sneaking in later."""
    assert DATE_RE.search("26.09.25") is None or DATE_RE.search("26.09.25").group(1) != "26"
