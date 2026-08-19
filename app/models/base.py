"""Declarative base, shared column types and mixins."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, MetaData, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

# Explicit naming convention so Alembic autogenerate produces stable, nameable
# constraints instead of anonymous ones that cannot be dropped cleanly.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class EnumString(TypeDecorator):
    """Store a `StrEnum` as VARCHAR but load it back as the enum member.

    A bare `String` column would round-trip to a plain `str`, which silently
    breaks identity comparisons (`status is InstanceStatus.RUNNING`) and any
    property defined on the enum (`status.is_transitional`) once a row has been
    loaded from the database rather than just constructed in memory.

    Native PG ENUM types are avoided deliberately: adding a value to one requires
    a migration and a table rewrite, and these vocabularies change often.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_class: type[StrEnum], length: int = 30, **kwargs: Any):
        self.enum_class = enum_class
        super().__init__(length=length, **kwargs)

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, self.enum_class):
            return value.value
        # Tolerate a plain string so raw queries and fixtures keep working, but
        # reject anything outside the vocabulary rather than persisting garbage.
        return self.enum_class(value).value

    def process_result_value(self, value: Any, dialect: Any) -> StrEnum | None:
        if value is None:
            return None
        return self.enum_class(value)


def new_uuid() -> str:
    return str(uuid.uuid4())


class UUIDPrimaryKey:
    id: Mapped[str] = mapped_column(primary_key=True, default=new_uuid)


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
