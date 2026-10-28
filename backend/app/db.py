from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase

from backend.app.config import Settings


class Base(DeclarativeBase):
    pass


def make_engine(settings: Settings, *, test: bool = False):
    return create_engine(
        settings.database_url(test=test),
        pool_pre_ping=True,
        isolation_level="READ COMMITTED",
        connect_args={"connect_timeout": 5, "read_timeout": 5, "write_timeout": 5},
    )
