"""Copy R2 lender logos to PostgreSQL, retaining keys and leaving originals intact.

Run after alembic upgrade head: python -m app.scripts.migrate_r2_logos
Requires requirements-r2-migration.txt and the old R2 environment settings (or .env).
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import select

from app.db import SessionLocal
from app.integrations.storage import store
from app.models import Lender


class LegacyR2Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    r2_account_id: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_bucket: str


def copy_logos(db, client, bucket: str) -> int:
    """The caller rolls back on failure; all copies commit together on success."""
    files = store(db)
    copied = 0
    rows = db.scalars(select(Lender).where(Lender.logo_key.is_not(None)).with_for_update()).all()
    for row in rows:
        key = row.logo_key
        if files.get(key) is not None:
            continue
        obj = client.get_object(Bucket=bucket, Key=key)
        body = obj["Body"]
        try:
            data = body.read()
        finally:
            body.close()
        content_type = obj.get("ContentType") or "image/webp"
        files.put(key, data, content_type)
        if files.get(key) != (data, content_type):
            raise RuntimeError("Copied logo verification failed; no changes committed.")
        copied += 1
    db.commit()
    return copied


def main() -> None:
    import boto3  # Optional migration dependency; the API never imports it.
    from botocore.config import Config

    settings = LegacyR2Settings()
    client = boto3.client(
        "s3",
        endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
        config=Config(connect_timeout=10, read_timeout=30, retries={"max_attempts": 3}),
    )
    with SessionLocal() as db:
        copied = copy_logos(db, client, settings.r2_bucket)
    print(f"Copied {copied} logos to PostgreSQL. R2 originals were left intact.")


if __name__ == "__main__":
    main()
