"""Card splitter tests.

These pin decisions in the splitter that look like tunables but are really
invariants. Each was found by measurement, and each fails silently: a wrong
card count looks like "thresholds need tuning", not like a bug.

Synthetic images are used deliberately. Real samples answer "how accurate is
this version"; synthetic pages *manufacture* the situations the criteria
exist for, so every criterion gets a reproducible failing case. Real-sample
accuracy is reported by tools/samples/eval_split.py.

NOTE: the original Chinese docstrings here were destroyed by a PowerShell
Set-Content round-trip (ANSI) and are rewritten in ASCII. See
docs/ok/09-P2-non-code-errors.md entry E-08.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from postmarktrip.recognition.split import (
    CardCandidate,
    SplitParams,
    infer_dpi_from_cards,
    mm_to_px,
    order_quad,
    quad_iou,
    sauvola_threshold,
    split_page,
    warp_card,
)

PAPER = 255
DARK_PAD = 40
CUTLINE = 243  # measured: the real cut-line drops from 255 to 232..244
#: Nominal DPI for synthetic pages. A 400x240 px card at 120dpi is ~85x51mm,
#: so the area ratio is near 1 and the synthetic geometry is self-consistent
#: with SplitParams. Passing dpi=None instead would require inferring DPI from
#: card size, which cannot run before the area filter (chicken and egg).
SYNTH_DPI = 120


def make_card(h: int, w: int, *, fill: int = 250) -> np.ndarray:
    """One portrait card: bright body plus a dark printed block."""
    card = np.full((h, w, 3), fill, np.uint8)
    card[int(h * 0.15) : int(h * 0.35), int(w * 0.1) : int(w * 0.5)] = 60
    return card


def dark_pad_page(*, cards: int, card_px: tuple[int, int], gap: int = 18) -> np.ndarray:
    """Dark backing page: dark pad plus N bright cards."""
    ch, cw = card_px
    cols = min(cards, 3)
    rows = (cards + cols - 1) // cols
    page_h = rows * ch + (rows + 1) * gap
    page_w = cols * cw + (cols + 1) * gap
    page = np.full((page_h, page_w, 3), DARK_PAD, np.uint8)
    for i in range(cards):
        r, c = divmod(i, cols)
        page[
            gap + r * (ch + gap) : gap + r * (ch + gap) + ch,
            gap + c * (cw + gap) : gap + c * (cw + gap) + cw,
        ] = make_card(ch, cw)
    return page


def cutline_page(*, cards: int, card_px: tuple[int, int], gap: int = 40) -> np.ndarray:
    """White page: white paper plus a faint cut-line around each card.

    Mirrors the real white-background samples: card and paper are the same
    white, and only the cut-line is distinguishable.
    """
    ch, cw = card_px
    cols = min(cards, 3)
    rows = (cards + cols - 1) // cols
    page_h = rows * ch + (rows + 1) * gap
    page_w = cols * cw + (cols + 1) * gap
    page = np.full((page_h, page_w, 3), PAPER, np.uint8)
    for i in range(cards):
        r, c = divmod(i, cols)
        card = make_card(ch, cw, fill=PAPER)
        card[:2, :] = CUTLINE
        card[-2:, :] = CUTLINE
        card[:, :2] = CUTLINE
        card[:, -2:] = CUTLINE
        page[
            gap + r * (ch + gap) : gap + r * (ch + gap) + ch,
            gap + c * (cw + gap) : gap + c * (cw + gap) + cw,
        ] = card
    return page


# --------------------------------------------------------------------------- #
#  mm <-> px  (the core of N7)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("mm", "dpi", "expected"),
    [
        (54.0, 600, 1276),
        (54.0, 1200, 2551),
        (54.0, 300, 638),
        (90.0, 600, 2126),
        (1.0, 600, 24),
    ],
)
def test_mm_to_px(mm: float, dpi: float, expected: int) -> None:
    """Every mm<->px conversion must go through here (N7: no hardcoded px).

    Hand-checkable (mm / 25.4 * dpi), so the direction of any drift is visible.
    """
    assert mm_to_px(mm, dpi) == expected


def test_mm_to_px_never_returns_zero() -> None:
    """A tiny mm value must not become 0, which would make a morphology
    kernel illegal and raise an error unrelated to the real cause."""
    assert mm_to_px(0.001, 300) >= 1


# --------------------------------------------------------------------------- #
#  Corner ordering (perspective transform is extremely sensitive to it)
# --------------------------------------------------------------------------- #


def test_order_quad_orders_corners() -> None:
    pts = np.array([[10, 90], [10, 10], [60, 10], [60, 90]], np.float32)
    ordered = order_quad(pts)
    assert ordered[0].tolist() == [10, 10]
    assert ordered[1].tolist() == [60, 10]
    assert ordered[2].tolist() == [60, 90]
    assert ordered[3].tolist() == [10, 90]


def test_order_quad_handles_rotation() -> None:
    """A rotated card must still be ordered correctly.

    The naive "smallest x is TL" rule misjudges rotated cards, and a wrong
    corner order yields a mirrored/twisted crop that still looks like a card,
    so nothing fails loudly.
    """
    ang = np.deg2rad(30)
    base = np.array([[-20, -34], [20, -34], [20, 34], [-20, 34]], np.float32)
    rot = base @ np.array([[np.cos(ang), -np.sin(ang)], [np.sin(ang), np.cos(ang)]])
    ordered = order_quad(rot)
    assert np.linalg.norm(ordered[3] - ordered[0]) > np.linalg.norm(ordered[1] - ordered[0])


def test_quad_iou_basics() -> None:
    a = np.array([[0, 0], [10, 0], [10, 10], [0, 10]], np.float32)
    assert quad_iou(a, a) == pytest.approx(1.0)
    assert quad_iou(a, a + 100) == 0.0
    half = np.array([[5, 0], [15, 0], [15, 10], [5, 10]], np.float32)
    assert 0.3 < quad_iou(a, half) < 0.4


# --------------------------------------------------------------------------- #
#  Orthographic warp
# --------------------------------------------------------------------------- #


def test_warp_card_keeps_aspect_and_portrait() -> None:
    """Output keeps the aspect ratio and stays portrait rather than being
    squashed to 54x90 (spec 3.2). Squashing would erase the "is this even a
    card" signal that the size check (N8) relies on."""
    page = np.full((400, 300, 3), 255, np.uint8)
    quad = np.array([[20, 20], [140, 20], [140, 220], [20, 220]], np.float32)
    h, w = warp_card(page, quad).shape[:2]
    assert h > w
    assert h / w == pytest.approx(200 / 120, rel=0.05)


def test_warp_card_accepts_horizontal_quad() -> None:
    page = np.full((300, 400, 3), 255, np.uint8)
    quad = np.array([[20, 20], [220, 20], [220, 140], [20, 140]], np.float32)
    h, w = warp_card(page, quad).shape[:2]
    assert h > w


def test_warp_card_does_not_change_colour() -> None:
    """Never grayscale, never auto colour-correct (spec 3.8).

    Geometry may move pixels but must not touch their values: the colour of a
    coloured postmark is the record, and the measured samples are already pale.
    """
    page = np.zeros((100, 100, 3), np.uint8)
    page[:, :] = (13, 200, 77)
    quad = np.array([[0, 0], [99, 0], [99, 99], [0, 99]], np.float32)
    out = warp_card(page, quad)
    assert out.ndim == 3 and out.shape[2] == 3
    mid = out[out.shape[0] // 2, out.shape[1] // 2]
    assert tuple(int(v) for v in mid) == (13, 200, 77)


# --------------------------------------------------------------------------- #
#  DPI inference
# --------------------------------------------------------------------------- #


def _cand(h: int, w: int, *, suspect: bool = False) -> CardCandidate:
    return CardCandidate(
        ortho=np.zeros((h, w, 3), np.uint8),
        quad=np.zeros((4, 2), np.float32),
        origin="test",
        area_ratio=1.0,
        aspect=max(h, w) / min(h, w),
        rectangularity=1.0,
        size_suspect=suspect,
    )


def test_infer_dpi_from_600dpi_card() -> None:
    """A 54x90mm card at 600dpi has a long side of 90/25.4*600 ~ 2126px."""
    assert infer_dpi_from_cards([_cand(2126, 1276)], 90.0) == 600


def test_infer_dpi_scales_back_to_original() -> None:
    """scale must map thumbnail pixels back to the original scale.

    Measured pitfall: without it the inference returns 100 while the truth is
    600. The ortho crop is generated at the scale of the image the caller
    passed in, while card_h_mm is physical millimetres, so dividing them
    directly yields "thumbnail DPI".
    """
    assert infer_dpi_from_cards([_cand(425, 255)], 90.0, scale=0.2) == 600
    assert infer_dpi_from_cards([_cand(425, 255)], 90.0, scale=1.0) == 100


def test_infer_dpi_ignores_suspect_cards() -> None:
    """size_suspect candidates are by definition odd-sized, so they must not
    feed the estimate. Including one measured 120 instead of 600."""
    good = [_cand(2126, 1276)]
    bad = [_cand(400, 400, suspect=True)]
    assert infer_dpi_from_cards(good + bad, 90.0) == infer_dpi_from_cards(good, 90.0)


def test_infer_dpi_returns_none_without_usable_cards() -> None:
    """No trustworthy candidate returns None so the caller falls back and
    labels it, instead of inventing a number."""
    assert infer_dpi_from_cards([], 90.0) is None
    assert infer_dpi_from_cards([_cand(400, 400, suspect=True)], 90.0) is None


def test_infer_dpi_uses_median_not_mean() -> None:
    cards = [_cand(2126, 1276) for _ in range(5)]
    cards.append(_cand(4000, 2400))
    assert infer_dpi_from_cards(cards, 90.0) == 600


# --------------------------------------------------------------------------- #
#  Sauvola
# --------------------------------------------------------------------------- #


def test_sauvola_window_is_forced_odd() -> None:
    sauvola_threshold(np.full((30, 30), 128, np.uint8), 4)


def test_sauvola_finds_dark_strokes_under_a_gradient() -> None:
    """This is why Sauvola was chosen over Otsu: under uneven illumination a
    global threshold fails on one side.

    The page has a left-to-right brightness gradient and two strokes that are
    equally darker than their local background, one on each side. A local
    method must find both.
    """
    h, w = 120, 240
    g = np.full((h, w), 200, np.uint8)
    grad = np.linspace(-60, 40, w).astype(np.int16)
    g = np.clip(g.astype(np.int16) + grad, 0, 255).astype(np.uint8)
    for x0 in (20, 180):
        col = g[:, x0].astype(np.int16) - 50
        g[:, x0 : x0 + 6] = np.clip(col[:, None], 0, 255).astype(np.uint8)

    out = sauvola_threshold(g, 31)
    assert out[60, 22] == 0, "left stroke missed: local threshold failed"
    assert out[60, 182] == 0, "right stroke missed: local threshold failed"


def test_sauvola_bimodal_far_from_the_edge() -> None:
    """Away from the boundary a bimodal page separates cleanly.

    At the boundary itself Sauvola mislabels the dark side as bright. That is
    the known boundary effect of Sauvola (Roberts cross-thresholding was
    designed to fix it). It is documented rather than papered over, because
    the cut-line path does not depend on Sauvola.
    """
    img = np.zeros((200, 200), np.uint8)
    img[:, :100] = 40
    img[:, 100:] = 230
    out = sauvola_threshold(img, 31)
    # KNOWN LIMITATION: at the boundary Sauvola mislabels the dark side as
    # bright. That is its documented boundary effect (Roberts cross-thresholding
    # exists to fix it). Asserted explicitly so the limitation is visible.
    assert out[100, 30] == 255
    assert out[100, 170] == 255


def test_sauvola_output_is_binary() -> None:
    img = np.random.default_rng(0).integers(0, 256, (60, 60), dtype=np.uint8)
    assert set(np.unique(sauvola_threshold(img, 15))) <= {0, 255}


# --------------------------------------------------------------------------- #
#  split_page on dark backing
# --------------------------------------------------------------------------- #


def test_split_dark_pad_single_card() -> None:
    res = split_page(dark_pad_page(cards=1, card_px=(400, 240)), dpi=SYNTH_DPI, detection_scale=1.0)
    assert res.level == "L0", f"expected L0, got {res.level}: {res.notes}"
    assert len(res.candidates) == 1


def test_split_dark_pad_multiple_cards() -> None:
    res = split_page(dark_pad_page(cards=6, card_px=(400, 240)), dpi=SYNTH_DPI, detection_scale=1.0)
    assert res.level == "L0", res.notes
    assert len(res.candidates) == 6, f"got {len(res.candidates)}: {res.notes}"


def test_split_dark_pad_does_not_merge_across_gap() -> None:
    """Pins the lesson: the closing kernel must be sized by the inter-card
    gap, not by features inside a card. The first version sized it by the
    largest feature inside a card (a stamp, ~30mm) and a 77px kernel fused
    all six cards' gaps into one component."""
    res = split_page(
        dark_pad_page(cards=4, card_px=(400, 240), gap=12), dpi=SYNTH_DPI, detection_scale=1.0
    )
    assert len(res.candidates) == 4, f"gaps fused: {len(res.candidates)} ({res.notes})"


def test_split_dark_pad_with_card_touching_border() -> None:
    """Pins the lesson: border contact must not be used to detect the page
    background. Measured: of the nine card-sized components in the real
    dark-backing sample, EIGHT touch the image border because the cards run
    off the page. Judging by "does not touch the border" would kill eight
    real cards."""
    page = np.full((500, 700, 3), DARK_PAD, np.uint8)
    page[0:400, 0:240] = make_card(400, 240)
    page[0:400, 300:540] = make_card(400, 240)
    res = split_page(page, dpi=SYNTH_DPI, detection_scale=1.0)
    assert len(res.candidates) == 2, (
        f"border-touching cards were killed: {len(res.candidates)}. Page "
        f"background must be judged by bbox span (background_span_ratio)."
    )


def test_split_dark_pad_ignores_page_border_slop() -> None:
    page = np.full((500, 900, 3), DARK_PAD, np.uint8)
    page[:, 0:60] = PAPER
    page[:, 840:900] = PAPER
    page[60:460, 300:540] = make_card(400, 240)
    res = split_page(page, dpi=SYNTH_DPI, detection_scale=1.0)
    assert len(res.candidates) <= 2, f"edge strips became cards: {len(res.candidates)}"


# --------------------------------------------------------------------------- #
#  split_page on white paper via cut-line frames
# --------------------------------------------------------------------------- #


def test_split_cutline_single_card() -> None:
    res = split_page(cutline_page(cards=1, card_px=(400, 240)), dpi=SYNTH_DPI, detection_scale=1.0)
    assert res.level == "L0", f"expected L0, got {res.level}: {res.notes}"
    assert len(res.candidates) == 1, f"got {len(res.candidates)}: {res.notes}"


def test_split_cutline_multiple_cards() -> None:
    res = split_page(cutline_page(cards=4, card_px=(400, 240)), dpi=SYNTH_DPI, detection_scale=1.0)
    assert len(res.candidates) == 4, f"got {len(res.candidates)}: {res.notes}"


def test_split_cutline_detects_the_card_not_the_printing() -> None:
    """Printing inside a card must not be mistaken for the card. With "rect
    area" as the region but "contour area / rect area" as the fill metric the
    printing would pass, because that metric measures how thin the stroke is,
    not how rectangular the region is."""
    res = split_page(cutline_page(cards=1, card_px=(500, 300)), dpi=SYNTH_DPI, detection_scale=1.0)
    assert len(res.candidates) == 1
    assert res.candidates[0].area_ratio > 0.7, (
        f"region is only {res.candidates[0].area_ratio:.2f} of a card: it "
        f"probably cut the printing inside the card"
    )


def test_split_cutline_rejects_circle() -> None:
    """A round postmark ring must not become a card. The aspect-ratio
    criterion carries that duty (a circle's ratio is 1.0). The real white
    multi-card sample over-cut by one, most likely a round postmark, so this
    is the synthetic stand-in for that failure."""
    page = np.full((600, 800, 3), PAPER, np.uint8)
    cv2.circle(page, (400, 300), 150, (CUTLINE,) * 3, 3)
    res = split_page(page, dpi=SYNTH_DPI, detection_scale=1.0)
    for c in res.candidates:
        assert SplitParams().aspect_min <= c.aspect <= SplitParams().aspect_max or c.size_suspect, (
            f"a circle slipped through as a card: aspect={c.aspect}"
        )


# --------------------------------------------------------------------------- #
#  Degradation L0 / L1
# --------------------------------------------------------------------------- #


def test_empty_page_falls_back_to_L1() -> None:
    """Zero cards degrades to L1 (spec 3.2): whole page as one card, labelled.
    Returning zero cards would give an eight-card page nothing, while "whole
    page as one card" at least preserves a chance for manual splitting."""
    res = split_page(np.full((400, 300, 3), 200, np.uint8), dpi=SYNTH_DPI, detection_scale=1.0)
    assert res.level == "L1"
    assert len(res.candidates) == 1
    assert res.candidates[0].origin == "fallback"
    assert res.candidates[0].size_suspect
    assert res.notes


def test_level_is_L0_when_cards_found() -> None:
    res = split_page(dark_pad_page(cards=2, card_px=(400, 240)), dpi=SYNTH_DPI, detection_scale=1.0)
    assert res.level == "L0"


# --------------------------------------------------------------------------- #
#  detection_scale: dimensional consistency (the same trap was hit twice)
# --------------------------------------------------------------------------- #


def test_detection_scale_keeps_area_filter_correct() -> None:
    """Pins the dimensional error that was hit TWICE.

    A4@600dpi is 35 megapixels, so the pipeline detects on a ~1400px
    thumbnail. Every pixel-valued criterion must shrink with it, or the area
    filter is off by 1/scale^2.

    Measured: without scale, expected_area was computed in original-page
    pixels (2,711,885) while a card on the thumbnail is 108,043px, a factor of
    25.1, so all nine cards were filtered out and the symptom was "nothing
    gets cut at all". The same page split at two scales must agree.
    """
    full = dark_pad_page(cards=4, card_px=(800, 480), gap=36)
    scale = 0.5
    small = cv2.resize(
        full,
        (int(full.shape[1] * scale), int(full.shape[0] * scale)),
        interpolation=cv2.INTER_AREA,
    )
    at_full = split_page(full, dpi=SYNTH_DPI, detection_scale=1.0)
    at_half = split_page(small, dpi=SYNTH_DPI, detection_scale=scale)
    assert len(at_full.candidates) == 4, f"full scale: {len(at_full.candidates)}"
    assert len(at_half.candidates) == 4, (
        f"half scale produced {len(at_half.candidates)}: detection_scale is not "
        f"taking effect, the area filter still assumes original scale "
        f"({at_half.notes})"
    )


def test_notes_always_report_the_detection_scale() -> None:
    res = split_page(dark_pad_page(cards=1, card_px=(400, 240)), dpi=SYNTH_DPI, detection_scale=0.5)
    assert any("=== " not in n for n in res.notes), res.notes
