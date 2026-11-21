# `mod-postventa` — Postventa

- **Owner:** @MathiasTL
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
| Backend | `public.py` | `PostventaService`, `provide_postventa_service`, `PostventaPort`, los DTO de detalle |
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
