"""Opening uploaded images safely.

A small file can describe an enormous picture (a "decompression bomb"): decoding it would use
gigabytes of memory and stop the server. Every upload is opened here first, which reads only the
image's header and refuses anything over MAX_PIXELS before a single pixel is decoded. Pillow's own
limit is set to the same value as a backstop for any other code path."""

import io
import warnings

from PIL import Image

# 40 megapixels: far above any phone photo or scanned receipt in normal use (phones save 12 MP
# by default), and about 160 MB at most to decode.
MAX_PIXELS = 40_000_000
TOO_LARGE = "This image is too large. Choose a photo under 40 megapixels."

Image.MAX_IMAGE_PIXELS = MAX_PIXELS
# Above the limit Pillow only warns until twice the limit: make it refuse straight away.
warnings.simplefilter("error", Image.DecompressionBombWarning)


class ImageTooLarge(ValueError):
    pass


def open_checked(data: bytes, *, longest_side: int | None = None) -> Image.Image:
    """The image, opened lazily (header only) and refused when its pixel count is too large.
    With `longest_side`, a JPEG is decoded at a reduced size straight away (far less memory)."""
    try:
        image = Image.open(io.BytesIO(data))
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageTooLarge(TOO_LARGE) from None
    width, height = image.size
    if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
        image.close()
        raise ImageTooLarge(TOO_LARGE)
    if longest_side and image.format == "JPEG":
        image.draft("RGB", (longest_side, longest_side))
    return image
