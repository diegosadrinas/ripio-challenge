from collections.abc import Generator

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, database_url: str) -> None:
        connect_args = {}
        if database_url.startswith("sqlite"):
            connect_args = {"check_same_thread": False}

        self.engine = create_engine(
            database_url,
            pool_pre_ping=True,
            future=True,
            connect_args=connect_args,
        )
        self._session_factory = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=self.engine,
            expire_on_commit=False,
            class_=Session,
        )

    def init_models(self) -> None:
        from app import models  # noqa: F401

        Base.metadata.create_all(bind=self.engine)

    def session(self) -> Session:
        return self._session_factory()


def get_db(request: Request) -> Generator[Session, None, None]:
    db = request.app.state.db.session()
    try:
        yield db
    finally:
        db.close()
