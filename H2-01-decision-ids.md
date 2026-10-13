# H2-01 — Decisión de IDs y convenciones BD

## Decisión (Sebastian) + ajuste H2-10
- **Supabase real: PostgreSQL 17.11** (verificado `SELECT version()` en H2-10; en H2-01 se asumía PG18).
- IDs: `server_default=text("uuidv7()")` en cada modelo, resuelto con **función SQL propia `public.uuidv7()`** creada en la migración `0001` (plpgsql + pgcrypto, bits versión 7 / variante RFC 9562). Sin tocar los 14 modelos.
- `set_updated_at()` + trigger `BEFORE UPDATE trg_<tabla>_updated_at` en las 9 tablas con `updated_at`, también en `0001` (autogenerate no detecta funciones/triggers).
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
