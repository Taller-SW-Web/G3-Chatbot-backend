# `mod-postventa` — Postventa

- **Owner:** @Taller-SW-Web/chatbot
- **Backend:** paquete `mod_postventa`, `basePath` `/devoluciones`, `/reclamos`, `/evidencias`, `/pedidos/{id}/csat`
- **Frontend:** carpeta `mod-postventa`, rutas `[/devolucion]`, `[/reclamo]`
- **Depende de:** `mod-orders` (solo por `public.py`)
- **Contrato:** `docs/openapi.json`, tag `mod-postventa`.

## Responsabilidad

Devoluciones, reclamos, carga de evidencias y encuesta CSAT contra G5 M2
Postventa, con idempotencia local (`claim_ref`, `return_ref`) y validación de
propiedad del pedido en Ventas. **No** anula pedidos ni reembolsa.

## Superficie pública

| Tipo | Archivo | Qué expone |
|---|---|---|
| Backend | `public.py` | `PostventaService`, `provide_postventa_service`, `PostventaPort`, `PostventaError`, los DTO de detalle y las etiquetas de estado (`map_claim_status_label`, `map_return_status_label`, `OPEN_CLAIM_STATES`) que `mod-chat` usa en los bloques |
| Frontend | `public.ts` | Tipos de devolución, reclamo y CSAT |

## Rutas

| Método | Ruta | Caso de uso |
|---|---|---|
| POST | `/api/v1/evidencias` | `PostventaService.upload_evidence` |
| POST | `/api/v1/devoluciones` | `PostventaService.create_return` |
| GET | `/api/v1/devoluciones` | `PostventaService.list_returns` |
| GET | `/api/v1/devoluciones/{devolucion_id}` | `PostventaService.get_return` |
| POST | `/api/v1/reclamos` | `PostventaService.create_claim` |
| GET | `/api/v1/reclamos` | `PostventaService.list_claims` |
| GET | `/api/v1/reclamos/{codigo}` | `PostventaService.get_claim` |
| POST | `/api/v1/pedidos/{pedido_id}/csat` | `PostventaService.create_csat` |

## Duplicados (SPEC-19/21)

Antes de crear un reclamo o una devolución se consulta el listado real del
cliente (`/me`). Si hay otro **abierto** del mismo pedido + motivo (devolución:
también las mismas líneas), responde `409 RECLAMO_DUPLICADO` /
`409 DEVOLUCION_DUPLICADA` con el código y estado existentes. El cliente puede
reintentar con `forzar: true` para registrar igual; `forzar` no se envía a G5.
Un caso ya `ATENDIDO`/`DERIVADO` (o `RECHAZADA`/`COMPLETADA`) no es duplicado.

El listado se recorre **página por página** (50 por página) hasta agotarlo; un
duplicado en la posición 51 también responde `409`. Si G5 falla o expira, la
creación continúa (fail-open, SPEC-19/21) y se registra un `warning` con el
`correlation_id`; el cliente no ve un error de duplicados.

## Evidencias y borradores (SPEC-19/21)

- Al registrar una devolución, las evidencias subidas en esa solicitud se ligan
  a `return_ref.return_id` en la **misma unidad de trabajo** (`evidence.return_id`)
  mediante `EvidencePostgresAdapter.asociar_devolucion`.
- Los borradores de reclamo y devolución viven en `conversation.context`
  (`postventaClaimDraft`, `postventaReturnDraft`) y expiran a las 24 h. El
  servicio los persiste cuando el caso de uso recibe `conversation_id`
  (`save_*_draft`), los recupera (`get_*_draft`) y los borra al registrar. El
  contrato HTTP no cambia.
- `python -m src.modules.mod_postventa.jobs.purge_drafts` borra las referencias
  de evidencia de borradores sin enviar con más de 24 h. El archivo en Ventas no
  se toca. El job es propio del módulo y se agenda fuera del worker compartido.

## CSAT (SPEC-24)

`POST /pedidos/{id}/csat` sigue delegando en Ventas y ahora marca
`order_ref.csat_sent` cuando Ventas responde 201 o `409 CSAT_ALREADY_SUBMITTED`,
para no volver a mostrar el bloque de calificación. Solo actualiza si existe
una fila local de `order_ref` para el pedido.

## Dependencias externas

- **G5 M2 Postventa** `POST /devoluciones`, `POST /reclamos`, `POST /csat`,
  `POST /devoluciones/evidencias/upload` con el token del titular.
- **G5 Ventas** solo para probar propiedad del pedido (`provide_sales_port`,
  reexportado por `mod-orders.public`).

## Pruebas

- Unitarias: `tests/unit/mod_postventa/`
- Integración: `tests/integration/mod_postventa/`
- Contrato: endpoint ↔ `docs/openapi.json` (tag `mod-postventa`)

## Reglas

- No importar `domain/`, `application/` ni `adapters/` de otro módulo.
- No tocar `shared/` sin label `cross-modulo`.
- Un PR de este módulo no modifica archivos de otro `mod-*`.
