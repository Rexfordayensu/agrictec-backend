"""The crop doctor (lib/ai/crop-diagnosis.ts).

This is a repeatable demonstration, not image recognition: the photo's name and size pick a
result from the admin's active diagnoses, so the same photo always gets the same answer. To use
a real plant-disease vision API, replace diagnose() and keep its signature."""

from app.schemas.farm import DiagnosisResultOut
from app.schemas.platform import DiagnosisContent

MAX_IMAGE_BYTES = 10 * 1024 * 1024
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}


def _fnv1a(text: str) -> int:
    """FNV-1a over UTF-16 code units, as the browser computed it."""
    value = 2166136261
    units = text.encode("utf-16-le")
    for i in range(0, len(units), 2):
        value = ((value ^ int.from_bytes(units[i : i + 2], "little")) * 16777619) & 0xFFFFFFFF
    return value


def diagnose(
    filename: str, size: int, catalog: list[DiagnosisContent], crop: str | None = None
) -> DiagnosisResultOut:
    active = [d for d in catalog if d.active]
    if not active:
        raise ValueError("The crop doctor is not available right now.")
    seed = _fnv1a(f"{filename}:{size}")
    for_crop = [d for d in active if d.crop == crop or not d.crop] if crop else active
    candidates = for_crop or active
    picked = candidates[seed % len(candidates)]
    if picked.confidence_min is not None and picked.confidence_max is not None:
        confidence = picked.confidence_min + ((seed >> 8) % (picked.confidence_max - picked.confidence_min + 1))
    else:
        confidence = picked.confidence
    return DiagnosisResultOut(
        disease=picked.disease,
        severity=picked.severity,
        confidence=confidence,
        treatment=picked.treatment,
        prevention=list(picked.prevention) if picked.prevention else None,
        crop=crop or picked.crop,
    )
