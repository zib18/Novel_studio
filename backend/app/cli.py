from contextlib import closing
"""python -m app.cli migrate | create-user | reset-password | backup"""
import argparse
import getpass
import os
from pathlib import Path
import sqlite3
from datetime import datetime, timezone
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.engine import make_url
from .auth import hash_password, normalize_email
from .config import DATA_DIR, Settings
from .database import build_session_factory, make_engine
from .models import User, WebSession


def migrate(settings=None):
    settings = settings or Settings()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["database_url"] = settings.database_url
    command.upgrade(config, "head")


def read_password():
    password = getpass.getpass("Пароль (не менее 12 символов): ")
    confirmation = getpass.getpass("Повторите пароль: ")
    if password != confirmation:
        raise ValueError("Пароли не совпадают")
    if not 12 <= len(password) <= 1024:
        raise ValueError("Пароль должен содержать от 12 до 1024 символов")
    return password


def create_user(db, email, password):
    email = normalize_email(email)
    if "@" not in email or len(email) > 320 or not 12 <= len(password) <= 1024:
        raise ValueError("Проверьте адрес и длину пароля")
    if db.scalar(select(User.id).limit(1)):
        raise ValueError("Автор уже создан; используйте reset-password для смены пароля")
    user = User(email=email, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    return user


def backup(settings, output=None):
    url = make_url(settings.database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise ValueError("Для PostgreSQL используйте pg_dump и защищённое хранилище резервных копий")
    source = Path(url.database).resolve()
    if not source.is_file():
        raise ValueError("База не найдена; сначала выполните migrate")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = Path(output).resolve() if output else DATA_DIR / "backups" / f"novel-{stamp}.db"
    if target == source:
        raise ValueError("Резервная копия не может заменять рабочую базу")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".partial")
    try:
        with closing(sqlite3.connect(str(source))) as original, closing(sqlite3.connect(str(temporary))) as destination:
            original.backup(destination)
            destination.execute("PRAGMA journal_mode=DELETE")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description="Управление Novel Studio")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("migrate")
    create = sub.add_parser("create-user")
    create.add_argument("--email", required=True)
    reset = sub.add_parser("reset-password")
    reset.add_argument("--email", required=True)
    copy = sub.add_parser("backup")
    copy.add_argument("--output")
    args = parser.parse_args(argv)
    settings = Settings()
    try:
        if args.action == "migrate":
            migrate(settings)
            print("Миграции применены.")
            return
        if args.action == "backup":
            print(backup(settings, args.output))
            return
        engine = make_engine(settings.database_url)
        factory = build_session_factory(engine)
        with factory() as db:
            if args.action == "create-user":
                user = create_user(db, args.email, read_password())
                print(f"Автор создан: {user.email}")
            else:
                user = db.scalar(select(User).where(User.email == normalize_email(args.email)))
                if user is None:
                    raise ValueError("Автор не найден")
                user.password_hash = hash_password(read_password())
                for session in db.scalars(select(WebSession).where(WebSession.user_id == user.id)):
                    db.delete(session)
                db.commit()
                print("Пароль изменён; прежние сеансы завершены.")
        engine.dispose()
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
