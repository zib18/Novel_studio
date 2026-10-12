from alembic import context
from app.config import Settings
from app.database import make_engine

config = context.config
database_url = config.attributes.get("database_url") or Settings().database_url


def run_migrations_offline():
    context.configure(url=database_url, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    engine = make_engine(database_url)
    with engine.connect() as connection:
        context.configure(connection=connection, render_as_batch=connection.dialect.name == "sqlite")
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
