from alembic import context
from sqlalchemy import create_engine

from ytagent.config import get_settings
from ytagent.models import Base

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=get_settings().env.database_url, target_metadata=target_metadata,
                      literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(get_settings().env.database_url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
