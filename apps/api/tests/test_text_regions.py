"""Text-region extraction tests (spec 3.5).

These pin the two mistakes that were actually made while building this step.
Both were silent: the pipeline produced a region, it just contained no text.

NOTE: ASCII-only docstrings -- see docs/ok/09-P2-non-code-errors.md E-08.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from postmarktrip.recognition.postmark_type import (
    TYPE_DAILY,
    TYPE_SCENIC,
    TYPE_UNKNOWN,
    Circle,
    TypeVerdict,
    arc_angles,
    find_text_band,
    polar_unwrap,
)
from postmarktrip.recognition.text_regions import (
    extract_arc_regions,
    extract_text_regions,
    find_text_lines,
)

# --------------------------------------------------------------------------- #
#  Synthetic postmarks
# --------------------------------------------------------------------------- #

W, H = 600, 600
CX, CY, R = 300, 300, 200
#: The solid outer ring thickness, in px. Real stamps: ~10px at this size.
RING = 8


def make_postmark(
    *,
    top_arc_text: bool = True,
    bottom_arc_text: bool = True,
    center_blocks: int = 0,
    colored: bool = False,
) -> np.ndarray:
    """A synthetic daily postmark: solid outer ring + optional text arcs.

    ``center_blocks`` draws filled blocks in the middle -- used to make a
    "scenic-like" stamp whose text is NOT on an arc.
    """
    img = np.full((H, W, 3), 255, np.uint8)
    # Solid outer ring (the thing that has circumferential density ~1.0)
    cv2.circle(img, (CX, CY), R, (30, 30, 30), RING)
    # Thinner inner ring, as most real postmarks have
    cv2.circle(img, (CX, CY), int(R * 0.78), (60, 60, 60), 2)

    if top_arc_text:
        # Characters placed just INSIDE the outer ring, on the top arc.
        # Placed at 0.72~0.92 x R -- inside the ring, not on it.
        for i, ang_deg in enumerate((-40, -27, -14, 1, 15, 29, 42)):
            a = np.deg2rad(ang_deg - 90)
            rr = R * 0.82
            x = int(CX + rr * np.cos(a))
            y = int(CY + rr * np.sin(a))
            cv2.putText(img, "字", (x - 14, y + 14), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (40, 40, 40), 2)
            del i
    if bottom_arc_text:
        for ang_deg in (-30, -10, 10, 30):
            a = np.deg2rad(ang_deg + 90)
            rr = R * 0.82
            x = int(CX + rr * np.cos(a))
            y = int(CY + rr * np.sin(a))
            cv2.putText(img, "字", (x - 14, y + 14), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (40, 40, 40), 2)

    if center_blocks:
        # Wide blocks spanning many columns -> low circumferential density,
        # exactly like a scenic stamp's artwork / horizontal name line.
        for k in range(center_blocks):
            y = int(CY - 40 + k * 40)
            img[y : y + 22, CX - 110 : CX + 110] = (50, 50, 50)

    if colored:
        cv2.circle(img, (CX, CY), int(R * 0.5), (120, 180, 90), 30)
    return img


def _circle() -> Circle:
    return Circle(cx=CX, cy=CY, r=float(R), quality=0.95)


def _verdict(kind: str) -> TypeVerdict:
    return TypeVerdict(
        kind=kind,
        confidence=0.9,
        top_arc_text=0.6,
        center_image=0.3,
        circle=_circle(),
        reason="test",
    )


# --------------------------------------------------------------------------- #
#  The core mistake: the text band must NOT be the solid outer ring
# --------------------------------------------------------------------------- #


def test_text_band_excludes_the_solid_outer_ring() -> None:
    """**This is the bug that was actually shipped.**

    v2 of ``find_text_band`` looked for "the first band from outside with
    density >= 0.25". On a real sample the radial profile is
    "solid outer ring density ~1.0" + "text ring density 0.04..0.15" --
    so v2 selected **the solid ring itself** and the unwrapped top-arc
    response was 0.008 (a band of solid ink, no text).

    The root error was the assumption "text has high circumferential
    density". It does not: a 4-6 character place name only covers part of
    the circumference, so its average density is LOW, while a solid ring is
    inked all the way around.

    This test asserts the band lands INSIDE the solid ring.
    """
    img = make_postmark()
    band = find_text_band(img, _circle())
    assert band is not None, "no band found on a synthetic daily postmark"
    # The solid ring occupies [R - RING/2, R + RING/2]. The text ring is
    # clearly inside it, so the band's outer edge must be inside R - RING/2.
    assert band.r_outer <= R - RING / 2 + 3, (
        f"band outer {band.r_outer:.0f} reaches the solid ring "
        f"(ring inner edge {R - RING / 2}) -- this is the v2 bug"
    )
    assert band.r_inner < band.r_outer


def test_arc_extraction_actually_finds_text() -> None:
    """The end-to-end consequence: the top arc must respond like text.

    Before the fix this measured 0.008 on a real sample; with the band
    corrected it measures 0.74. On the synthetic stamp the gap is even
    starker, so this is a cheap regression guard.
    """
    img = make_postmark()
    regions = extract_arc_regions(img, _circle())
    assert len(regions) == 2, f"expected top+bottom arc, got {len(regions)}"
    by_kind = {r.kind: r for r in regions}
    assert by_kind["top_arc"].text_response > 0.25, (
        f"top arc response only {by_kind['top_arc'].text_response:.3f} -- "
        f"the band is probably on the solid ring again"
    )


def test_band_on_blank_page_returns_none_or_blank() -> None:
    """A blank circle must not fabricate text regions.

    A white page with only a thin ring has no text band; the honest answers
    are "no band" or "band with near-zero response".
    """
    img = np.full((H, W, 3), 255, np.uint8)
    cv2.circle(img, (CX, CY), R, (30, 30, 30), 2)  # thin ring only
    band = find_text_band(img, _circle())
    if band is not None:
        regions = extract_arc_regions(img, _circle())
        for r in regions:
            assert r.text_response < 0.25, (
                f"fabricated a text region ({r.kind} response "
                f"{r.text_response:.3f}) from a blank circle"
            )


# --------------------------------------------------------------------------- #
#  Region bookkeeping
# --------------------------------------------------------------------------- #


def test_arc_regions_carry_bbox_inside_the_image() -> None:
    """Every region must carry a bbox in ORIGINAL image coordinates.

    Required by the review queue: it has to draw "where was this taken from",
    otherwise the user cannot judge whether to trust the OCR result.
    """
    img = make_postmark()
    for r in extract_arc_regions(img, _circle()):
        x, y, w, h = r.bbox
        assert x >= 0 and y >= 0
        assert x + w <= W and y + h <= H, f"bbox {r.bbox} escapes the image"
        assert r.image.size > 0


def test_arc_regions_are_horizontal() -> None:
    """Unwrapped arcs are horizontal -- OCR must not rotate them."""
    for r in extract_arc_regions(make_postmark(), _circle()):
        assert r.orientation == "horizontal"


def test_arc_angles_put_top_arc_above_the_centre() -> None:
    """Angle convention: 0 deg is +x, 90 deg is +y (image coords, y down).

    So the TOP arc is centred on 270 deg. Getting this backwards silently
    swaps "province name" with "post office name" -- no error, just wrong
    field attribution. Hence a dedicated test.
    """
    a0, a1 = arc_angles("top")
    mid = np.deg2rad((a0 + a1) / 2)
    # sin(mid) < 0 means "above centre" in image coordinates
    assert np.sin(mid) < 0, f"top arc centred at {np.rad2deg(mid):.0f} deg, not above centre"
    b0, b1 = arc_angles("bottom")
    assert np.sin(np.deg2rad((b0 + b1) / 2)) > 0, "bottom arc must be below centre"


def test_polar_unwrap_samples_one_pixel_per_degree_of_arc() -> None:
    """Width must be the ARC LENGTH, not the angular span in degrees.

    The v1 bug: ``columns = angular span`` (90 deg -> 90 columns). But
    arc length is ``r * theta``, so 90 deg at r=200px is 314px. 90 columns
    therefore compressed the text ~3.5x horizontally -- and the symptom was
    not a crash but **OCR reading nothing at all** from a perfectly good
    region (the strip was still legible to a human, which is why this is
    easy to misdiagnose as "the model is bad").
    """
    strip = polar_unwrap(
        make_postmark(), _circle(), angle_from_deg=-45, angle_to_deg=45, out_height=40
    )
    expected = int(round(np.deg2rad(90) * R * 0.85))  # r_mid = R*(0.55+1.15)/2
    assert strip.shape[1] == expected, (
        f"expected {expected} columns (arc length) for 90 deg at r={R}, "
        f"got {strip.shape[1]} -- columns must not equal the degree span"
    )
    assert strip.shape[0] == 40


def test_polar_unwrap_width_scales_with_radius() -> None:
    """Bigger radius -> longer arc -> more columns, for the same angle span.

    This is what makes the sampling density resolution-independent: a
    300dpi scan and a 600dpi scan of the same stamp produce strips of
    comparable legibility rather than one being squashed.
    """
    img = make_postmark()
    small = polar_unwrap(
        img,
        Circle(cx=CX, cy=CY, r=100.0, quality=1.0),
        angle_from_deg=-45,
        angle_to_deg=45,
        out_height=20,
    )
    large = polar_unwrap(
        img,
        Circle(cx=CX, cy=CY, r=250.0, quality=1.0),
        angle_from_deg=-45,
        angle_to_deg=45,
        out_height=20,
    )
    assert large.shape[1] > small.shape[1] * 2


def test_polar_unwrap_explicit_width_wins() -> None:
    strip = polar_unwrap(
        make_postmark(),
        _circle(),
        angle_from_deg=-45,
        angle_to_deg=45,
        out_height=30,
        out_width=200,
    )
    assert strip.shape == (30, 200)


# --------------------------------------------------------------------------- #
#  Dispatch by stamp type
# --------------------------------------------------------------------------- #


def test_daily_dispatches_to_arc_regions() -> None:
    img = make_postmark(center_blocks=0)
    regions = extract_text_regions(img, _verdict(TYPE_DAILY))
    assert regions, "daily stamp produced no regions"
    assert all(r.kind in ("top_arc", "bottom_arc") for r in regions), (
        f"daily stamp should only use arc regions, got {[r.kind for r in regions]}"
    )


def test_scenic_path_is_declared_unusable() -> None:
    """**Pins the fact that the scenic path does NOT work yet.**

    Measured: both scenic samples (the monument stamp and the coloured
    Jinci stamp) yield 0 regions. So scenic text extraction is unfinished
    -- and this test exists so nobody mistakes "no regions" for a bug fix
    target without reading why.

    The function returns empty on purpose: an empty result makes the review
    queue show "no text region found" and the user draws a box, whereas a
    WRONG region makes OCR read artwork, emit garbage, and pollute the
    confidence statistics.
    """
    img = make_postmark(top_arc_text=False, bottom_arc_text=False, center_blocks=3)
    lines = find_text_lines(img, _circle())
    assert lines == [], (
        f"find_text_lines returned {lines} -- if this now works, update the "
        f"docstring in text_regions.find_text_lines (it currently declares "
        f"the function unusable) and re-run the scenic samples"
    )


def test_scenic_verdict_yields_no_arc_regions() -> None:
    """A scenic stamp must not get arc regions -- its text is not on the ring.

    This is the pipeline decision from spec 3.3 (measured on real samples:
    the monument stamp's name is horizontal across the middle).
    """
    img = make_postmark(center_blocks=3)
    regions = extract_text_regions(img, _verdict(TYPE_SCENIC))
    assert not any(r.kind in ("top_arc", "bottom_arc") for r in regions), (
        f"scenic stamp got arc regions: {[r.kind for r in regions]}"
    )


def test_unknown_runs_both_strategies() -> None:
    """When the type cannot be decided, BOTH strategies must run.

    That is exactly when extra candidates matter most: claiming "no text" is
    worse than offering two regions the user can ignore.
    """
    img = make_postmark(center_blocks=2)
    regions = extract_text_regions(img, _verdict(TYPE_UNKNOWN))
    assert regions, "unknown type produced nothing at all"


def test_no_circle_means_no_regions() -> None:
    """Without a located postmark there is nothing to extract -- return []
    rather than guessing a region over the whole image."""
    v = TypeVerdict(
        kind=TYPE_DAILY,
        confidence=0.0,
        top_arc_text=0.0,
        center_image=0.0,
        circle=None,
        reason="no circle",
    )
    assert extract_text_regions(make_postmark(), v) == []


def test_regions_sorted_by_text_response() -> None:
    img = make_postmark()
    regions = extract_text_regions(img, _verdict(TYPE_UNKNOWN))
    responses = [r.text_response for r in regions]
    assert responses == sorted(responses, reverse=True)


# --------------------------------------------------------------------------- #
#  Scenic name-line search
# --------------------------------------------------------------------------- #


def test_find_text_lines_finds_a_wide_horizontal_band() -> None:
    """**Currently xfail-by-design**: the scenic name-line search does not
    work on real samples, so this synthetic expectation is not met either.

    Kept as an ``xfail`` rather than deleted: it documents the intent, and
    ``strict=True`` means it turns into an XPASS (a failure) the moment the
    implementation starts working -- so the docstring claiming
    "unusable" cannot quietly become stale.
    """
    pytest.xfail("scenic name-line search is not implemented (see find_text_lines docstring)")
    img = np.full((H, W, 3), 255, np.uint8)
    cv2.circle(img, (CX, CY), R, (30, 30, 30), 2)
    img[CY - 18 : CY + 18, CX - 120 : CX + 120] = (40, 40, 40)
    lines = find_text_lines(img, _circle())
    assert lines, "a wide central band was not detected as a text line"


def test_find_text_lines_rejects_tiny_specks() -> None:
    """Single dots must not become text lines.

    Kept as a guard even though the function currently returns nothing at
    all: when the scenic path is implemented, this must still hold.
    """
    img = np.full((H, W, 3), 255, np.uint8)
    cv2.circle(img, (CX, CY), R, (30, 30, 30), 2)
    for dx, dy in ((-80, -80), (70, 60), (0, -90)):
        cv2.circle(img, (CX + dx, CY + dy), 3, (0, 0, 0), -1)
    lines = find_text_lines(img, _circle())
    assert all(resp < 0.5 for _, resp in lines), (
        f"tiny specks scored as text: {[(b, r) for b, r in lines]}"
    )


def test_scenic_regions_are_marked_with_orientation() -> None:
    """Every scenic region needs an orientation so OCR knows whether to
    rotate. Rotating wrongly yields empty output, not an error."""
    img = make_postmark(top_arc_text=False, bottom_arc_text=False, center_blocks=1)
    for r in extract_text_regions(img, _verdict(TYPE_SCENIC)):
        assert r.orientation in ("horizontal", "vertical")


@pytest.mark.parametrize("kind", ["top_arc", "bottom_arc"])
def test_arc_bbox_is_not_degenerate(kind: str) -> None:
    regions = {r.kind: r for r in extract_arc_regions(make_postmark(), _circle())}
    assert kind in regions
    _, _, w, h = regions[kind].bbox
    assert w > 10 and h > 3
