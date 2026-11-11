"""Private files in PostgreSQL, in the same transaction as the record using them.

Files are served through the existing API permission checks, never public URLs or local disk.
The caller owns the session and commits (or rolls back) the entire change.
"""

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import StoredFile


class DatabaseStore:
    def __init__(self, db: Session) -> None:
        self.db = db

    def put(self, key: str, data: bytes, content_type: str) -> None:
        statement = insert(StoredFile).values(key=key, data=data, content_type=content_type)
        self.db.execute(statement.on_conflict_do_update(
            index_elements=[StoredFile.key],
            set_={"data": statement.excluded.data, "content_type": statement.excluded.content_type},
        ))

    def get(self, key: str) -> tuple[bytes, str] | None:
        row = self.db.get(StoredFile, key)
        return (row.data, row.content_type) if row is not None else None

    def delete(self, key: str) -> None:
        self.db.execute(delete(StoredFile).where(StoredFile.key == key))


def store(db: Session) -> DatabaseStore:
    return DatabaseStore(db)
