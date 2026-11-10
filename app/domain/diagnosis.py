"""The crop doctor's limits (lib/image/compress.ts). The photo is read by Claude
(app/integrations/claude.py): without ANTHROPIC_API_KEY the crop doctor is unavailable. It never
makes up a diagnosis."""

MAX_IMAGE_BYTES = 10 * 1024 * 1024
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
