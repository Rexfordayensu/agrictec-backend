from datetime import datetime
from typing import Annotated, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, PlainSerializer, StringConstraints
from pydantic.alias_generators import to_camel

from app.domain.clock import iso

# Serialized like JavaScript's toISOString(): "2026-10-27T09:30:00.000Z".
Timestamp = Annotated[datetime, PlainSerializer(iso, return_type=str, when_used="json-unless-none")]


def Text(max_length: int, min_length: int = 1):  # noqa: N802 - reads like a type
    """A trimmed string of 1..max_length characters."""
    return Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=min_length, max_length=max_length)
    ]


class CamelModel(BaseModel):
    """Accepts and returns camelCase keys (snake_case is accepted too)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)


class Message(CamelModel):
    detail: str


class Confirmation(CamelModel):
    """A typed confirmation for a destructive action ("DELETE", the farmer's email, ...)."""

    confirmation: str


T = TypeVar("T")


class Paged(CamelModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int
    page_count: int
