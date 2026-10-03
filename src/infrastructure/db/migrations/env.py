"""Alembic env (H2-01). target_metadata = Base.metadata para autogenerate."""
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Permite `from src.adapters...` cuando se ejecuta `alembic` desde la raíz del backend.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from src.infrastructure.db.connection import Base  # noqa: E402

# Importa modelos para que autogenerate los detecte (convención models/__init__.py).
# Las 14 tablas (nombres en inglés) tienen modelo poblado desde H2-02..H2-07.
try:
    import src.adapters.outbound.persistence.models  # noqa: F401,E402
except Exception:
    pass

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# DATABASE_URL manda sobre alembic.ini (no commitear credenciales).
db_url = os.getenv("DATABASE_URL")
if db_url:
    # Alembic corre en sync; si viene asyncpg lo adapta a psycopg2 para el offline/online.
    # En H2-10 se usa Supabase con peer adecuado; aquí solo se deja el override.
    sync_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    config.set_main_option("sqlalchemy.url", sync_url)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
