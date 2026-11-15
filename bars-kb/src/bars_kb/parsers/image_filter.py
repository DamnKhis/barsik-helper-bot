"""Pillow-based validation for images embedded in documents.

Document exports regularly contain tiny system icons, paperclip icons for
attached video files, and square "missing image" placeholders. Sending those
to a vision model wastes tokens and adds noise to retrieval, so every parser
runs images through this filter before they become chunks.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

MIN_WIDTH = 50
MIN_HEIGHT = 50

# Near-square, small images with a flat background are typical placeholders
# or file-type icons (paperclip, broken-image cross, toolbar glyphs).
PLACEHOLDER_MAX_SIDE = 320
PLACEHOLDER_MAX_COLORS = 80
PLACEHOLDER_MIN_DOMINANT = 0.55
PLACEHOLDER_RATIO_MIN = 0.8
PLACEHOLDER_RATIO_MAX = 1.25


@dataclass(frozen=True)
class ImageVerdict:
    """Result of validating one embedded image."""

    ok: bool
    reason: str
    width: int = 0
    height: int = 0


def inspect_image(
    data: bytes,
    *,
    min_width: int = MIN_WIDTH,
    min_height: int = MIN_HEIGHT,
) -> ImageVerdict:
    """Return whether an image is worth sending to the vision model.

    The check is deliberately conservative: it rejects only images that are
    unreadable, too small, or match the placeholder/icon heuristic. Anything
    ambiguous (large screenshots, diagrams, photos) is kept.
    """

    if not data:
        return ImageVerdict(False, "empty image")

    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            width, height = image.size
            if width < min_width or height < min_height:
                return ImageVerdict(
                    False,
                    f"too small: {width}x{height} < {min_width}x{min_height}",
                    width,
                    height,
                )
            if _looks_like_placeholder(image):
                return ImageVerdict(
                    False,
                    f"placeholder/icon: {width}x{height}",
                    width,
                    height,
                )
            return ImageVerdict(True, "ok", width, height)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        return ImageVerdict(False, f"unreadable: {type(exc).__name__}")


def _looks_like_placeholder(image: Image.Image) -> bool:
    width, height = image.size
    ratio = width / height if height else 0.0
    if not PLACEHOLDER_RATIO_MIN <= ratio <= PLACEHOLDER_RATIO_MAX:
        return False
    if max(width, height) > PLACEHOLDER_MAX_SIDE:
        return False

    sample = image.convert("RGB").resize((32, 32))
    colors = sample.getcolors(maxcolors=32 * 32)
    if not colors:
        return False

    total = 32 * 32
    distinct = len(colors)
    dominant = max(count for count, _ in colors) / total

    # Solid or almost solid squares are blank placeholders, not content.
    if distinct == 1:
        return True
    return distinct <= PLACEHOLDER_MAX_COLORS and dominant >= PLACEHOLDER_MIN_DOMINANT
