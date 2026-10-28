"""Geometry-invariant tests: ``postmark_diameter_hint_px`` and the quad bbox.

Two functions in this project exist purely to hold an invariant that was
derived from measurement, and both were, until now, only covered
incidentally by integration tests:

``postmark_diameter_hint_px``
    Removes the DPI dependence from the postmark-circle size prior. Two
    rounds of circle-detection fixes were needed to get here, and the
    function is what makes the fix *not* require knowing the scan DPI.

``_quad_bbox``
    Converts a numpy corner array into a JSON-serialisable Python-int box.
    An integration test caught this one: ``numpy.float32`` cannot be
    ``json.dumps``-ed, and the observation ``bbox`` column is JSONB, so the
    failure would only have appeared at DB-write time.

NOTE: ASCII-only docstrings -- see docs/ok/09-P2-non-code-errors.md E-08.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from postmarktrip.recognition.pipeline import _quad_bbox
from postmarktrip.recognition.postmark_type import POSTMARK_DIAMETER_MM
from postmarktrip.recognition.text_regions import (
    CARD_ASPECT,
    CARD_ASPECT_TOL,
    CARD_H_MM,
    CARD_W_MM,
    postmark_diameter_hint_px,
)

# --------------------------------------------------------------------------- #
#  The diameter hint: a ratio, not a measurement
# --------------------------------------------------------------------------- #


def test_hint_is_the_physical_ratio_for_a_real_card() -> None:
    """A full card's short side times 25/54 gives the postmark diameter.

    The point is that DPI cancels: ``dpi = short_side / 54mm`` and
    ``diameter = 25mm * dpi``, so the mm figures divide out and the answer
    depends only on pixels. That is why a wrong DPI can no longer corrupt
    the size prior.
    """
    # 1080x1800 px card == 54x90mm at 508dpi
    card = np.zeros((1800, 1080, 3), np.uint8)
    hint = postmark_diameter_hint_px(card)
    assert hint == pytest.approx(1080 * 25.0 / 54.0, rel=1e-6)


def test_hint_is_dpi_invariant() -> None:
    """**This is the whole point of the function.**

    The same physical card scanned at 300dpi and at 1200dpi must produce
    hints in the SAME ratio to the image, because the hint is computed from
    image proportions alone.
    """
    small = np.zeros((900, 540, 3), np.uint8)  # 54x90mm at 254dpi
    large = np.zeros((3600, 2160, 3), np.uint8)  # 54x90mm at 1016dpi
    h_small = postmark_diameter_hint_px(small)
    h_large = postmark_diameter_hint_px(large)
    assert h_large / h_small == pytest.approx(4.0, rel=1e-6), (
        "the hint did not scale with the image -- it is not DPI-invariant"
    )


def test_hint_handles_landscape_cards() -> None:
    """A card rotated 90 degrees must still give a sensible hint.

    The assertion is deliberately loose (same order of magnitude) because
    for a landscape card the short side is the HEIGHT, and the physical
    ratio 25/54 is defined against the card's WIDTH. Rather than silently
    applying the wrong ratio, the function falls back to the short-side
    fraction -- and this test pins that it produces something plausible
    instead of garbage.
    """
    landscape = np.zeros((1080, 1800, 3), np.uint8)
    hint = postmark_diameter_hint_px(landscape)
    assert hint > 0
    assert 0.2 * 1080 < hint < 0.8 * 1080, f"implausible hint {hint} for a landscape card"


def test_hint_falls_back_for_a_non_card_image() -> None:
    """A page scan (A4-ish aspect) is not a card, so the card ratio must not
    be applied blindly -- but a prior is still better than none, because
    with no prior at all the circle search grabs the whole card (measured:
    r=1736px on a full-page scan)."""
    page = np.zeros((7014, 5072, 3), np.uint8)
    hint = postmark_diameter_hint_px(page)
    assert hint > 0
    assert hint < min(page.shape[:2]), "hint exceeds the image"


def test_hint_prefers_the_card_ratio_within_tolerance() -> None:
    """Just inside the aspect tolerance the card ratio must apply; just
    outside it must not."""
    w = 1000
    h_ok = int(w * (CARD_ASPECT + CARD_ASPECT_TOL * 0.5))
    h_bad = int(w * (CARD_ASPECT + CARD_ASPECT_TOL * 3.0))
    inside = postmark_diameter_hint_px(np.zeros((h_ok, w, 3), np.uint8))
    outside = postmark_diameter_hint_px(np.zeros((h_bad, w, 3), np.uint8))
    assert inside == pytest.approx(w * POSTMARK_DIAMETER_MM / CARD_W_MM, rel=1e-6)
    assert outside != pytest.approx(w * POSTMARK_DIAMETER_MM / CARD_W_MM, rel=1e-6)


def test_hint_uses_short_side_for_a_portrait_card() -> None:
    """For a portrait card the short side is the width -- and the ratio
    25/54 is defined against the width, so the two agree."""
    card = np.zeros((1800, 1080, 3), np.uint8)
    assert postmark_diameter_hint_px(card) == pytest.approx(
        min(1800, 1080) * POSTMARK_DIAMETER_MM / CARD_W_MM, rel=1e-6
    )


def test_hint_on_a_tiny_image_returns_zero_not_a_crash() -> None:
    """Degenerate inputs must not raise: the pipeline calls this on every
    card, including tiny or malformed crops from a bad split."""
    assert postmark_diameter_hint_px(np.zeros((4, 4, 3), np.uint8)) == 0.0
    assert postmark_diameter_hint_px(np.zeros((1, 1, 3), np.uint8)) == 0.0


def test_card_ratio_constants_are_consistent() -> None:
    """The 25/54 ratio is only meaningful if the card constants are right."""
    assert CARD_W_MM == 54.0
    assert CARD_H_MM == 90.0
    assert POSTMARK_DIAMETER_MM == 25.0
    assert pytest.approx(90 / 54) == CARD_ASPECT


# --------------------------------------------------------------------------- #
#  Quad bbox: numpy in, JSON-ready Python ints out
# --------------------------------------------------------------------------- #


def test_quad_bbox_returns_python_ints() -> None:
    """**The bug an integration test caught.**

    ``quad[:, 0].min()`` returns ``numpy.float32``. It computes, compares
    and prints fine -- but cannot be ``json.dumps``-ed, and the observation
    ``bbox`` column is JSONB. So the failure would surface at DB-write time,
    far from the cause.
    """
    quad = np.array([[10.5, 20.5], [110.5, 20.5], [110.5, 220.5], [10.5, 220.5]], np.float32)
    box = _quad_bbox(quad)
    for v in box:
        assert type(v) is int, f"{v!r} is {type(v).__name__}, not int"
    json.dumps({"bbox": box})  # must not raise


def test_quad_bbox_is_json_serialisable_for_awkward_floats() -> None:
    """Values that are float32-representable but not exactly integers must
    still produce ints and survive json.dumps."""
    quad = np.array([[0.1, 0.2], [99.9, 0.2], [99.9, 199.8], [0.1, 199.8]], np.float32)
    box = _quad_bbox(quad)
    assert all(type(v) is int for v in box)
    json.dumps(box)


def test_quad_bbox_covers_the_whole_quad() -> None:
    """The bbox must enclose every corner -- used by the review UI to draw
    where a value came from."""
    quad = np.array([[5.0, 7.0], [105.0, 3.0], [110.0, 203.0], [2.0, 207.0]], np.float32)
    x, y, w, h = _quad_bbox(quad)
    for px, py in quad:
        assert x <= px <= x + w, f"corner {px} outside [{x}, {x + w}]"
        assert y <= py <= y + h, f"corner {py} outside [{y}, {y + h}]"


def test_quad_bbox_is_never_degenerate() -> None:
    """A zero-size bbox would break the review UI's rectangle. Degenerate
    quads (all corners identical, e.g. from a failed detection) must still
    give at least 1x1."""
    degenerate = np.zeros((4, 2), np.float32)
    x, y, w, h = _quad_bbox(degenerate)
    assert w >= 1 and h >= 1


def test_quad_bbox_handles_an_empty_quad() -> None:
    """A missing quad must give a neutral box rather than raising."""
    assert _quad_bbox(np.zeros((0, 2), np.float32)) == (0, 0, 0, 0)
    assert _quad_bbox(np.array([])) == (0, 0, 0, 0)


def test_quad_bbox_rounds_outward() -> None:
    """Outward rounding (floor the min, ceil the max) keeps the box a
    superset; rounding inward would clip a pixel of the card edge."""
    quad = np.array([[10.9, 20.9], [110.1, 20.9], [110.1, 220.1], [10.9, 220.1]], np.float32)
    x, y, w, h = _quad_bbox(quad)
    assert x <= 10.9 and y <= 20.9
    assert x + w >= 110.1 and y + h >= 220.1
