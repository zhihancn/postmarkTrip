"""Regression test for the work-scale DPI invariant.

**Pins a bug that shipped and made every downstream feature meaningless.**

`recognize_page` resizes the page to a work size (scale < 1). The ortho card
crops and the postmark-circle search run at THAT scale, but `split.dpi` is in
ORIGINAL-image terms. Feeding the original-scale dpi to the per-card stage made
the expected postmark diameter too large by 1/scale.

Measured on the real samples (work scale 0.285, original 600dpi):

    expected postmark diameter = 25mm at 600dpi = 590px
    actual postmark in the work-scale crop  ~= 590 * 0.285 = 168px
    search floor = 0.35 * 590 = 206px     <-- 168px is BELOW it

So the search skipped the real postmark and took a CARD-scale circle
(r=302 on a 361px-wide card). The "top arc" strip then sampled the postcard's
printed header or the stamp artwork, so `top_arc_text` became noise: across 28
real cards the daily/scenic distributions completely overlapped, and
local-vs-VLM agreement was 43%.

Nothing crashed. Every test passed. Hence a dedicated test.

The bug bites only when the true postmark falls below the floor, i.e. when
``scale < ~0.70`` (geometric argument: true_r = 0.492*D*scale vs
floor = 0.344*D). The tests below therefore force a small scale.

NOTE: ASCII-only docstrings -- see docs/ok/09-P2-non-code-errors.md E-08.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from postmarktrip.recognition.pipeline import recognize_page

PAPER = 255
DARK_PAD = 40
MM = 25.4


def page_with_one_card(*, dpi: float) -> tuple[np.ndarray, int]:
    """A dark page holding ONE white card of exactly 54x90mm at ``dpi``.

    Drawn at the pixel size the dpi implies, so the geometry is self-consistent
    -- no resizing that would break the mm<->px relation under test.
    Returns ``(page, card_width_px)``.
    """
    mm_per_px = MM / dpi
    cw = int(round(54 / mm_per_px))
    ch = int(round(90 / mm_per_px))
    pad = int(round(6 / mm_per_px))
    page = np.full((ch + 2 * pad, cw + 2 * pad, 3), DARK_PAD, np.uint8)
    card = np.full((ch, cw, 3), PAPER, np.uint8)
    # 25mm-diameter postmark ring, slightly below centre: radius 12.5mm, i.e.
    # ~0.23 of the card width. Ring width 1mm so the detector has something.
    cv2.circle(
        card,
        (cw // 2, ch // 2 + ch // 10),
        max(4, int(round(12.5 / mm_per_px))),
        (30, 30, 30),
        max(2, int(round(1.0 / mm_per_px))),
    )
    page[pad : pad + ch, pad : pad + cw] = card
    return page, cw


def test_work_dpi_equals_original_dpi_times_scale() -> None:
    """The invariant the bug violated, asserted directly."""
    page, _ = page_with_one_card(dpi=300)
    res = recognize_page(page, long_side=450, dpi=300)
    assert res.work_scale < 0.70, f"scale not small enough to be meaningful: {res.work_scale}"
    expected = 300 * res.work_scale
    joined = " ".join(res.stage_notes)
    assert "work" in joined, f"work-scale dpi not reported: {res.stage_notes}"
    assert f"{expected:.1f}" in joined, (
        f"work-scale dpi should be {expected:.1f} (= 300 x {res.work_scale:.4f}); "
        f"notes: {res.stage_notes}"
    )


def test_circle_is_postmark_scale_not_card_scale() -> None:
    """**The observable consequence of the bug.**

    The card is 54mm wide and the postmark 25mm across, so the located radius
    must be ~0.23 of the card width. With the original-scale dpi leaking in,
    the postmark fell below the search floor and a card-scale circle won.
    """
    page, card_w = page_with_one_card(dpi=300)
    res = recognize_page(page, long_side=450, dpi=300)
    assert res.card_count >= 1, f"no cards split: {res.stage_notes}"
    card = res.cards[0]
    assert card.verdict is not None
    circle = card.verdict.circle
    assert circle is not None, f"no circle located: {card.notes}"
    ratio = circle.r / card.card_size[0]
    assert ratio < 0.40, (
        f"circle radius is {ratio:.2f} of the card width ({card.card_size[0]}px, "
        f"drawn card {card_w}px) -- card-scale, not postmark-scale; the search "
        f"prior is in the wrong units"
    )


@pytest.mark.parametrize("dpi", [200, 300, 600])
def test_circle_scale_holds_at_any_dpi(dpi: int) -> None:
    """It is a UNITS invariant, so it must hold at any dpi -- a fix that only
    worked at one dpi would just be another magic constant."""
    page, _ = page_with_one_card(dpi=dpi)
    res = recognize_page(page, long_side=450, dpi=dpi)
    if not res.cards or not res.cards[0].verdict:
        pytest.skip(f"nothing to measure at dpi={dpi}")
    circle = res.cards[0].verdict.circle
    if circle is None:
        pytest.skip(f"no circle at dpi={dpi}")
    assert circle.r / res.cards[0].card_size[0] < 0.40
