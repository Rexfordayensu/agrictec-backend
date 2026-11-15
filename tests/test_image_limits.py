"""A small file can describe an enormous picture: every upload is refused before it is decoded
when it is over the pixel limit, so one file can't use up the server's memory."""

import io
import json

import pytest
from PIL import Image

from app.domain.images import MAX_PIXELS, ImageTooLarge, open_checked
from app.integrations.claude import prepare_photo
from app.services.lending import _clean_logo
from app.services.transactions import clean_file
from app.core.errors import AppError
from tests.conftest import API, receipt_png, signup


def huge_png() -> bytes:
    """A one-colour 8000 x 6000 picture (48 MP): a few kilobytes, but ~190 MB once decoded."""
    out = io.BytesIO()
    Image.new("1", (8000, 6000)).save(out, format="PNG", optimize=True)
    return out.getvalue()


def test_the_limit_is_checked_from_the_header_before_decoding():
    data = huge_png()
    assert len(data) < 200_000 and 8000 * 6000 > MAX_PIXELS
    with pytest.raises(ImageTooLarge):
        open_checked(data)
    with pytest.raises(AppError, match="too large"):
        clean_file(data)
    with pytest.raises(AppError, match="too large"):
        _clean_logo(data)
    with pytest.raises(ImageTooLarge):
        prepare_photo(data, "image/png")
    # A normal receipt still goes through.
    cleaned, kind = clean_file(receipt_png(3))
    assert kind == "image/webp" and cleaned


def test_a_huge_receipt_upload_is_refused_with_a_clear_message():
    farmer = signup("huge.image@example.com")
    body = {"direction": "in", "counterparty": "Buyer", "amount": "40.00", "date": "2026-11-14",
            "network": "MTN", "reference": "MP261114.1200.T00099"}
    r = farmer.post(f"{API}/me/momo", data={"data": json.dumps(body)},
                    files={"receipt": ("big.png", huge_png(), "image/png")})
    assert r.status_code == 400 and "too large" in r.json()["detail"]


def test_pillows_own_warning_also_reads_as_too_large(monkeypatch):
    """On the server the warning is an error (pytest relaxes warning filters, so force it here)."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with pytest.raises(AppError, match="too large"):
            clean_file(huge_png())
