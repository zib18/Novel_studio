from pathlib import Path
from fastapi import Request
from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    options = {}
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        if parsed.database and parsed.database != ":memory:":
            Path(parsed.database).resolve().parent.mkdir(parents=True, exist_ok=True)
        options["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(url, pool_pre_ping=True, **options)
    if parsed.get_backend_name() == "sqlite":
        @event.listens_for(engine, "connect")
        def sqlite_options(connection, _):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            if parsed.database != ":memory:":
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()
    return engine


def build_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


def get_db(request: Request):
    with request.app.state.session_factory() as db:
        yield db

