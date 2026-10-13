"""Copy a read-only SQLite snapshot into an empty migrated database.

Run with API and worker stopped:
    python -m app.transfer --source /tmp/novel.db
The target is NOVEL_DATABASE_URL. Existing sessions are omitted; unfinished jobs
are cancelled without API calls. No tables are created or cleared here.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import DateTime, create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool
from app.config import Settings
from app.database import Base, make_engine
from app import models as core_models
from app.generation import models as generation_models

BATCH_SIZE = 100
SKIPPED_TABLES = {"web_sessions"}
DEFERRED_LINKS = {
    "chapters": ("current_revision_id", "approved_revision_id"),
    "revisions": ("parent_revision_id",),
    "bible_entries": ("supersedes_id",),
}


class TransferError(RuntimeError):
    """Import refusal or failed transfer, without private row contents."""


def migration_heads():
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    heads = set(ScriptDirectory.from_config(config).get_heads())
    if not heads:
        raise TransferError("В исходном коде не найдены версии миграций.")
    return heads


def readonly_source_engine(path):
    uri = path.resolve().as_uri() + "?mode=ro"

    def connect():
        connection = sqlite3.connect(uri, uri=True, timeout=30)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    return create_engine("sqlite+pysqlite://", creator=connect, poolclass=NullPool, hide_parameters=True)


def check_schema(connection, expected_heads, label):
    inspector = inspect(connection)
    actual_tables = set(inspector.get_table_names())
    expected_tables = set(Base.metadata.tables) | {"alembic_version"}
    if actual_tables != expected_tables:
        raise TransferError(
            f"{label}: состав таблиц отличается от текущего приложения. "
            "Примените миграции; неизвестные таблицы не будут пропущены молча."
        )
    actual_heads = set(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
    if actual_heads != expected_heads:
        raise TransferError(f"{label}: версия Alembic не совпадает с текущим кодом.")
    for table in Base.metadata.tables.values():
        actual_columns = {item["name"] for item in inspector.get_columns(table.name)}
        if actual_columns != set(table.columns.keys()):
            raise TransferError(f"{label}: структура таблицы {table.name} не совпадает с кодом.")


def insertion_order():
    tables = {name: table for name, table in Base.metadata.tables.items() if name not in SKIPPED_TABLES}
    pending = set(tables)
    order = []
    while pending:
        ready = []
        for name in sorted(pending):
            deferred = set(DEFERRED_LINKS.get(name, ()))
            dependencies = {
                foreign_key.column.table.name
                for column in tables[name].columns
                if column.name not in deferred
                for foreign_key in column.foreign_keys
            }
            if dependencies & pending:
                continue
            if dependencies & SKIPPED_TABLES:
                raise TransferError("Сохраняемая таблица зависит от пропускаемых сессий.")
            ready.append(name)
        if not ready:
            raise TransferError("Обнаружен новый цикл связей: обновите модуль переноса.")
        for name in ready:
            pending.remove(name)
            order.append(tables[name])
    return order


def normalize_row(table, row):
    # SQLite timestamps are the application's UTC values, without timezone markers.
    for column in table.columns:
        value = row.get(column.name)
        if isinstance(column.type, DateTime) and isinstance(value, datetime) and value.tzinfo is None:
            row[column.name] = value.replace(tzinfo=timezone.utc)
    return row


def transfer_database(source, settings=None):
    settings = settings or Settings()
    source_path = Path(source).resolve()
    if not source_path.is_file():
        raise TransferError("Файл SQLite не найден.")
    target_url = make_url(settings.database_url)
    dialect = target_url.get_backend_name()
    if dialect not in {"sqlite", "postgresql"}:
        raise TransferError("Поддерживаются только PostgreSQL и SQLite.")
    if dialect == "sqlite":
        if (not target_url.database or target_url.database == ":memory:"
                or target_url.query.get("mode") == "memory" or "uri" in target_url.query):
            raise TransferError("Для SQLite требуется постоянная база с обычным файловым URL.")
        if Path(target_url.database).resolve() == source_path:
            raise TransferError("Источник и целевая база не могут быть одним файлом.")
    heads = migration_heads()
    order = insertion_order()
    source_engine = readonly_source_engine(source_path)
    target_engine = None
    counts = {}
    cancelled_jobs = 0
    try:
        with source_engine.connect() as original:
            # Explicit BEGIN: sqlite3 legacy mode does not start a transaction for SELECT.
            original.exec_driver_sql("BEGIN")
            check_schema(original, heads, "Источник")
            if original.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
                raise TransferError("Источник содержит нарушенные внешние связи.")
            skipped_sessions = original.scalar(
                select(func.count()).select_from(Base.metadata.tables["web_sessions"])
            )
            target_engine = make_engine(settings.database_url)
            target_engine.hide_parameters = True
            with target_engine.connect() as destination, destination.begin():
                if dialect == "sqlite":
                    destination.exec_driver_sql("BEGIN IMMEDIATE")
                else:
                    tables = sorted(set(Base.metadata.tables) | {"alembic_version"})
                    quoted = ", ".join(destination.dialect.identifier_preparer.quote(name) for name in tables)
                    destination.exec_driver_sql("LOCK TABLE " + quoted + " IN ACCESS EXCLUSIVE MODE")
                check_schema(destination, heads, "Целевая база")
                for table in Base.metadata.tables.values():
                    if destination.execute(select(table.c[next(iter(table.c.keys()))]).limit(1)).first():
                        raise TransferError(f"Целевая база не пуста: таблица {table.name}. Данные не изменены.")

                for table in order:
                    copied = 0
                    rows = original.execute(select(table)).mappings()
                    try:
                        for batch in rows.partitions(BATCH_SIZE):
                            prepared = []
                            for row in batch:
                                item = normalize_row(table, dict(row))
                                if table.name == "generation_jobs":
                                    item["lease_owner"] = None
                                    item["lease_until"] = None
                                    item["active_chapter_id"] = None
                                    if item["status"].casefold() in {"running", "queued"}:
                                        item["status"] = "cancelled"
                                        item["stage"] = "cancelled"
                                        item["cancel_requested"] = True
                                        notice = "Задача отменена при переносе базы. Запустите новую задачу явно."
                                        item["error"] = (item["error"] + "\n" if item["error"] else "") + notice
                                        item["updated_at"] = datetime.now(timezone.utc).isoformat()
                                        cancelled_jobs += 1
                                for column in DEFERRED_LINKS.get(table.name, ()):
                                    item[column] = None
                                prepared.append(item)
                            destination.execute(table.insert(), prepared)
                            copied += len(prepared)
                    finally:
                        rows.close()
                    counts[table.name] = copied

                # Referenced records now exist, including self-references. Preserve dates.
                for name, deferred in DEFERRED_LINKS.items():
                    table = Base.metadata.tables[name]
                    primary_key = tuple(table.primary_key.columns)
                    if len(primary_key) != 1:
                        raise TransferError("Для отложенной связи ожидается простой первичный ключ.")
                    key = primary_key[0]
                    preserved = tuple(column.name for column in table.columns if column.onupdate is not None)
                    columns = (key,) + tuple(table.c[name] for name in deferred + preserved)
                    rows = original.execute(select(*columns)).mappings()
                    try:
                        for row in rows:
                            item = normalize_row(table, dict(row))
                            if not any(item[name] is not None for name in deferred):
                                continue
                            values = {name: item[name] for name in deferred + preserved}
                            destination.execute(table.update().where(key == item[key.name]).values(**values))
                    finally:
                        rows.close()
            return {"copied": counts, "skipped_sessions": skipped_sessions,
                    "cancelled_jobs": cancelled_jobs, "alembic_heads": sorted(heads)}
    except TransferError:
        raise
    except (SQLAlchemyError, ValueError, TypeError, sqlite3.Error, ImportError):
        raise TransferError(
            "Перенос не подтверждён; источник не изменён. Целевая база изменяется одной транзакцией. "
            "Проверьте доступность базы, миграции и целостность копии. Если связь оборвалась "
            "при подтверждении транзакции, проверьте целевую базу перед повторным запуском."
        ) from None
    finally:
        source_engine.dispose()
        if target_engine is not None:
            target_engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Перенос Novel Studio из SQLite в пустую базу")
    parser.add_argument("--source", required=True, type=Path, help="Согласованная резервная копия SQLite")
    args = parser.parse_args(argv)
    try:
        result = transfer_database(args.source)
    except TransferError as error:
        parser.exit(1, f"Ошибка: {error}\n")
    print("Перенос завершён. Старые сессии не перенесены; требуется новый вход.")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
