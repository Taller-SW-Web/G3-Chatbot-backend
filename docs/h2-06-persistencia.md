# H2-06 — Persistencia de mensajes y evidencias

Asignación: `../G3-Chatbot-specs/odd/tasks/hito-2.md`, H2-06 (Alonso).
Esquema: `../G3-Chatbot-specs/docs/modelo-datos.md`, tablas `mensaje` y `evidencia`.

## Contratos

Los adapters reciben la `AsyncSession` compartida de H2-01
(`expire_on_commit=False`). `guardar` hace `add` y `flush`; el llamador controla
commit y rollback y valida la pertenencia de conversación y devolución.

| Adapter | Operación | Resultado |
|---|---|---|
| `MensajePostgresAdapter` | `guardar(mensaje)` | Fila persistida; texto previamente redactado y argumentos validados. |
| `MensajePostgresAdapter` | `listar_recientes(conversacion_id, limite=50)` | Últimos 1–50 mensajes en orden cronológico. Usar 12 para contexto LLM. |
| `EvidenciaPostgresAdapter` | `guardar(evidencia)` | Referencia al archivo alojado por Ventas. |
| `EvidenciaPostgresAdapter` | `listar_borrador(conversacion_id)` | Evidencias de esa conversación aún sin devolución. |
| `EvidenciaPostgresAdapter` | `asociar_devolucion(evidencia_id, conversacion_id, devolucion_id)` | Fila asociada o `None` si no corresponde; admite reintento a la misma devolución e impide reasignación. |

`bloques` y `argumentos` son objetos JSONB, no arrays; `None` se guarda como SQL
NULL. El ensamblador del turno convierte los bloques al formato de la API.
La asociación de evidencia comparte transacción con el INSERT de `devolucion_ref`.
La URL de evidencia no se envía al LLM.

## Dependencias para integrar

- **Mathias / H2-07:** publicar `conversacion`; integrar el adapter de mensajes
  con el de conversaciones. La redacción previa corresponde al filtro de Hito 3.
- **David / H2-04:** publicar `devolucion_ref`; el flujo de upload y limpieza de
  borradores a 24 h pertenece a SPEC-21 completo.
- **Sebastian / H2-10:** generar la migración con todos los modelos e instalar
  `set_actualizado_en()` y su trigger en `evidencia`. `server_onupdate` no crea
  el trigger. Autogenerate necesita los modelos relacionados para resolver las FK.

## Pruebas

Desde la raíz del backend, con Python 3.12:

```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
.venv/Scripts/python.exe -m pytest -q
```

Las pruebas unitarias usan sesiones simuladas y las de esquema compilan DDL
PostgreSQL con metadata aislada. La integración necesita `TEST_DATABASE_URL`
explícita con driver `postgresql+asyncpg`, PostgreSQL 18 y permiso para crear
esquemas. Sin esa variable se omite; no lee `.env` ni usa `DATABASE_URL`.

```powershell
.venv/Scripts/python.exe -m pytest -m integration -q
```

Cada prueba revierte su esquema exclusivo, datos y trigger. Sus padres mínimos
no sustituyen la prueba conjunta con los modelos reales ni la migración H2-10.
