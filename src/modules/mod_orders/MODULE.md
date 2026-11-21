# `mod-orders` — Pedidos y seguimiento

- **Owner:** @MathiasTL
- **Backend:** paquete `mod_orders`, `basePath` `/pedidos`
- **Frontend:** carpeta `mod-orders`, rutas `[/pedidos]`, `[/pedidos/[tab]]`, `[/seguimiento]`
- **Depende de:** `mod-checkout` (solo por `public.py`)
- **Contrato:** `docs/openapi.json`, tag `mod-orders`.

## Responsabilidad

Listado y detalle de pedidos del cliente, seguimiento con G2 Despacho y
reenvío de la confirmación por correo (SPEC-16..18). **No** crea pedidos
(`mod-checkout`) ni gestiona devoluciones o reclamos (`mod-postventa`).

## Superficie pública

| Tipo | Archivo | Qué expone |
|---|---|---|
| Backend | `public.py` | `OrdersService`, `provide_orders_service`, `NotificationService`/`NotificationError`, `map_order_status_label`, `sanitize_tracking_payload` |
| Frontend | `public.ts` | Tipos de pedido, seguimiento e hitos |

`provide_sales_port`, `provide_checkout_snapshot` y `provide_confirmation_outbox`
se reutilizan desde `mod-checkout.public` (dependencia declarada). El snapshot es
el único acceso a `order_ref`/`checkout`/`payment_attempt`; el despacho se
resuelve por la capacidad `dispatch_port`.

## Rutas

| Método | Ruta | Caso de uso |
|---|---|---|
| GET | `/api/v1/pedidos` | `OrdersService.list_orders` |
| GET | `/api/v1/pedidos/{pedido_id}` | `OrdersService.get_order_detail` |
| GET | `/api/v1/pedidos/{pedido_id}/seguimiento` | `OrdersService.get_order_tracking` |
| POST | `/api/v1/pedidos/{pedido_id}/reenviar-confirmacion` | `OrdersService.resend_confirmation` |

## Dependencias externas

- **G5 Ventas** `GET /pedidos/me`, `GET /pedidos/{id}` con el token del titular.
- **G2 Despacho** `GET /seguimientos/pedidos/{id}` con service token, vía
  capacidad `dispatch_port`.
- **SMTP** (`SMTP_HOST`) o el `FakeEmailSender` en memoria.
- Tabla `notification` (propia); `order_ref`/`checkout`/`payment_attempt` y el
  outbox se leen/escriben a través de `mod-checkout.public`.

## Capacidades que registra

| Capacidad | Consumidor | Motivo |
|---|---|---|
| `order_confirmation` | `mod-checkout`, worker de outbox | el correo de confirmación pertenece a este módulo |

## Pruebas

- Unitarias: `tests/unit/mod_orders/`
- Integración: `tests/integration/mod_orders/`
- Contrato: endpoint ↔ `docs/openapi.json` (tag `mod-orders`)

## Reglas

- No importar `domain/`, `application/` ni `adapters/` de otro módulo.
- No tocar `shared/` sin label `cross-modulo`.
- Un PR de este módulo no modifica archivos de otro `mod-*`.
