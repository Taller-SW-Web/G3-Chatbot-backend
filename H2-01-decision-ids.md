# H2-01 — Decisión de IDs y convenciones BD

## Decisión (Sebastian)
- **PostgreSQL 18 en Supabase + Python 3.12.7** (confirmado por equipo).
- IDs: `uuid DEFAULT uuidv7()` **nativo de PG18** con `server_default=text("uuidv7()")` en cada modelo.
- No se usa `uuid6`/`uuid_utils` ni función SQL propia. Si Supabase resultara no ser PG18, el fallback sería UUID v7 en app, sin cambiar `modelo-datos.md`.
- Engine **async** (`create_async_engine` + `asyncpg` + `AsyncSession`), acorde a FastAPI + httpx async del proyecto.

## Convención models/__init__.py
- Todo modelo hereda de `src.infrastructure.db.connection.Base`.
- Todo modelo se importa en `src/adapters/outbound/persistence/models/__init__.py`.
- Si no se importa ahí, `alembic revision --autogenerate` no lo detecta (falla silenciosa típica).
- H2-03..H2-07 deben agregar su import en ese `__init__.py`, sin generar migraciones.

## Regla snapshot jsonb
- Snapshots de módulos externos en `jsonb` con nombres reales del contrato (`product_id`, `precio_regular`, `channel_id`, etc.).
- Aplica a `cart_item`, `order_ref`, `claim_ref`, `return_ref` (ver `docs/modelo-datos.md`).
- Enums como `text + CHECK`, nunca `ENUM` Postgres. `timestamptz` UTC con `created_at DEFAULT now()`.

## Versionado migraciones
- `alembic.ini`: `script_location = src/infrastructure/db/migrations`, `file_template = %%(rev)s_%%(slug)s`.
- Primera migración `0001`, luego `0002`, ... con `--rev-id` explícito. Nunca editar/renumerar mergeadas.
- Solo Sebastian ejecuta `alembic revision --autogenerate --rev-id 0001` en H2-10 cuando los 14 modelos estén en `development`.
