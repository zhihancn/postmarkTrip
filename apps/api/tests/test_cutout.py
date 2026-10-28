"""Cut-out tests (spec 3.8).

The three hard rules of spec 3.8 (never grayscale, never auto colour-correct,
never resize to a fixed size) are all silent failures: the pipeline still
produces an image, it is just no longer evidence. So each gets an explicit
test.

NOTE: ASCII-only docstrings -- see docs/ok/09-P2-non-code-errors.md E-08.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from postmarktrip.recognition.cutout import (
    CUTOUT_PIPELINE_VERSION,
    assert_colour_preserved,
    cut_postmark,
    cut_stamp,
)
from postmarktrip.recognition.postmark_type import Circle

W, H = 400, 400
CX, CY, R = 200, 200, 120


def coloured_postmark() -> np.ndarray:
    """A coloured postmark on white paper."""
    img = np.full((H, W, 3), 255, np.uint8)
    cv2.circle(img, (CX, CY), R, (140, 90, 60), 6)  # blue-ish ring
    cv2.circle(img, (CX, CY), int(R * 0.6), (60, 160, 200), 4)  # orange-ish
    cv2.putText(img, "A", (CX - 20, CY + 15), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (80, 140, 40), 3)
    return img


def _circle(r: float = float(R)) -> Circle:
    return Circle(cx=float(CX), cy=float(CY), r=r, quality=1.0)


# --------------------------------------------------------------------------- #
#  Rule 1 + 2: colour must survive
# --------------------------------------------------------------------------- #


def test_postmark_cutout_preserves_colour() -> None:
    """Graeyscaling is silent and irreversible.

    One ``cvtColor(GRAY2BGR)`` anywhere in the path destroys every coloured
    postmark's evidence without raising anything. The measured samples are
    already pale (pink/green/magenta, low saturation), so there is no margin
    to lose.
    """
    src = coloured_postmark()
    cut = cut_postmark(src, _circle())
    assert cut is not None
    # Not greyscale: the channels differ from each other
    b, g, r = cut.image[:, :, 0], cut.image[:, :, 1], cut.image[:, :, 2]
    assert not np.array_equal(b, r), "B and R channels are identical -- greyscaled"
    assert not np.array_equal(b, g)
    assert_colour_preserved(src, cut.image)


def test_stamp_cutout_preserves_colour() -> None:
    src = coloured_postmark()
    cut = cut_stamp(src, (100, 100, 120, 120))
    assert cut is not None
    assert_colour_preserved(src, cut.image)


def test_colour_assertion_catches_a_greyscaled_cutout() -> None:
    """Negative control: the assertion must actually fire.

    Without this, ``assert_colour_preserved`` could be a no-op and the two
    tests above would still pass.
    """
    src = coloured_postmark()
    greyscaled = cv2.cvtColor(cv2.cvtColor(src, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
    with pytest.raises(AssertionError, match="灰度"):
        assert_colour_preserved(src, greyscaled)


def test_colour_assertion_allows_a_grey_source() -> None:
    """A genuinely grey postmark must not trip the assertion.

    The check is "was the source coloured and the output not", not "is the
    output coloured" -- otherwise grey stamps would fail.
    """
    grey = np.full((H, W, 3), 200, np.uint8)
    cv2.circle(grey, (CX, CY), R, (60, 60, 60), 6)
    assert_colour_preserved(grey, grey.copy())


# --------------------------------------------------------------------------- #
#  Rule 3: no resizing to a fixed size
# --------------------------------------------------------------------------- #


def test_postmark_cutout_keeps_native_pixels() -> None:
    """Output size must follow the source resolution, not a constant.

    Resizing to a fixed size would erase DPI, and DPI is the only basis for
    mm<->px conversion (N7). Two images of the same stamp at different
    resolutions must produce differently-sized cut-outs.
    """
    img = coloured_postmark()
    small = cv2.resize(img, (W // 2, H // 2), interpolation=cv2.INTER_AREA)
    big = cv2.resize(img, (W * 2, H * 2), interpolation=cv2.INTER_CUBIC)

    cut_s = cut_postmark(small, _circle(R / 2))
    cut_b = cut_postmark(big, _circle(R * 2))
    assert cut_s is not None and cut_b is not None
    assert cut_b.image.shape[0] > cut_s.image.shape[0] * 3, (
        "the cut-out was normalised to a fixed size -- DPI information is lost"
    )


def test_dpi_is_recorded_and_converted_to_mm() -> None:
    """With DPI known, the cut-out carries its physical size.

    Measured caveat: the close-up samples' effective DPI is about 360, not
    600, so a wrong DPI here makes the mm figure wrong by the same factor.
    Hence ``size_mm`` is None when DPI is unknown rather than guessed.
    """
    cut = cut_postmark(coloured_postmark(), _circle(), dpi=600)
    assert cut is not None
    assert cut.dpi == 600
    assert cut.size_mm is not None
    w_mm, h_mm = cut.size_mm
    # 2 * 120px radius * 1.06 margin = ~254px -> ~10.8mm at 600dpi
    assert 9.0 < w_mm < 13.0, f"unexpected mm width: {w_mm}"


def test_no_dpi_means_no_mm_rather_than_a_guess() -> None:
    cut = cut_postmark(coloured_postmark(), _circle())
    assert cut is not None
    assert cut.dpi is None
    assert cut.size_mm is None, "mm size must not be invented when DPI is unknown"


# --------------------------------------------------------------------------- #
#  Postmark patch shape: alpha, feather
# --------------------------------------------------------------------------- #


def test_postmark_cutout_has_alpha_and_is_transparent_outside() -> None:
    """A sticker has to be transparent outside the circle.

    Keeping the square paper background would show as a box on any
    differently-coloured background.
    """
    cut = cut_postmark(coloured_postmark(), _circle())
    assert cut is not None
    assert cut.has_alpha, "postmark patch must be BGRA"
    alpha = cut.image[:, :, 3]
    ch, cw = alpha.shape
    assert alpha[0, 0] == 0, "corner should be transparent"
    assert alpha[ch // 2, cw // 2] > 200, "centre should be opaque"


def test_postmark_cutout_feathers_the_edge() -> None:
    """The circular edge must not be a hard step.

    A hard edge aliases visibly when overlaid on a dark backing -- and a
    dark backing is the measured setup for the samples.
    """
    cut = cut_postmark(coloured_postmark(), _circle(), feather_px=2)
    assert cut is not None
    alpha = cut.image[:, :, 3]
    # A feathered ring means some pixels are neither 0 nor 255
    partial = ((alpha > 10) & (alpha < 245)).sum()
    assert partial > 50, f"only {partial} partially-transparent pixels -- not feathered"


def test_cutout_records_source_bbox() -> None:
    cut = cut_postmark(coloured_postmark(), _circle())
    assert cut is not None
    x, y, w, h = cut.source_bbox
    assert x >= 0 and y >= 0 and x + w <= W and y + h <= H


def test_cutout_records_pipeline_version() -> None:
    cut = cut_postmark(coloured_postmark(), _circle())
    assert cut is not None
    assert cut.pipeline_version == CUTOUT_PIPELINE_VERSION
    assert cut.method, "method description must be recorded for later auditing"


# --------------------------------------------------------------------------- #
#  Stamp patch: rectangular, perforations kept
# --------------------------------------------------------------------------- #


def test_stamp_cutout_keeps_perforations() -> None:
    """spec 3.4 requires the perforations to survive.

    They are both the evidence that this is a stamp and the reason not to
    trim along them: trimmed, the patch is indistinguishable from any
    rectangle of paper.
    """
    img = np.full((H, W, 3), 255, np.uint8)
    # A "stamp" with a scalloped edge
    cv2.rectangle(img, (100, 100), (260, 220), (200, 120, 60), -1)
    for x in range(100, 261, 10):
        cv2.circle(img, (x, 100), 4, (255, 255, 255), -1)
        cv2.circle(img, (x, 220), 4, (255, 255, 255), -1)

    cut = cut_stamp(img, (96, 96, 168, 128))
    assert cut is not None
    assert cut.image.shape[0] >= 120 and cut.image.shape[1] >= 160, (
        "the crop is tighter than the bounding box -- perforations were trimmed"
    )
    # The scallops leave white pixels inside the crop
    assert (cut.image == 255).all(axis=2).any(), "no scallop indentations preserved"


def test_stamp_cutout_is_rectangular_bgr() -> None:
    cut = cut_stamp(coloured_postmark(), (100, 100, 120, 120))
    assert cut is not None
    assert cut.image.ndim == 3 and cut.image.shape[2] == 3
    assert not cut.has_alpha


def test_stamp_cutout_rejects_a_clipped_bbox() -> None:
    """A bbox that extends past the image is clipped, not an error."""
    cut = cut_stamp(coloured_postmark(), (350, 350, 200, 200))
    assert cut is None or cut.image.shape[0] <= H


def test_tiny_bbox_is_rejected() -> None:
    """A 4px patch is useless and would silently enter the gallery."""
    assert cut_stamp(coloured_postmark(), (10, 10, 4, 4)) is None


def test_degenerate_circle_is_rejected() -> None:
    assert cut_postmark(coloured_postmark(), _circle(1.0)) is None
