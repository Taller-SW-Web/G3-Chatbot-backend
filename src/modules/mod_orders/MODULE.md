# `mod-orders` — Pedidos y seguimiento

- **Owner:** @Taller-SW-Web/chatbot
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
| Backend | `public.py` | `OrdersService`, `provide_orders_service`, `NotificationService`/`NotificationError`, `map_order_status_label`, `sanitize_tracking_payload`, `cancellation_message` |
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

`GET /pedidos` acepta `page` con `1 <= page <= 1000` y `pageSize` hasta 50; una
página fuera de rango responde `400 VALIDACION` sin consultar la base ni G5
(#255).

## Dependencias externas

- **G5 Ventas** `GET /pedidos/me`, `GET /pedidos/{id}` con el token del titular.
- **G2 Despacho** `GET /seguimientos/pedidos/{id}` con service token, vía
  capacidad `dispatch_port`.
- **SMTP** (`EMAIL_MODE=smtp` + `SMTP_HOST`) o el `FakeEmailSender` en memoria
  (`EMAIL_MODE=fake`). Producción sin `SMTP_HOST` falla al arrancar salvo que
  `EMAIL_MODE=fake` sea explícito; `/health` publica el modo efectivo.
- Tabla `notification` (propia); `order_ref`/`checkout`/`payment_attempt` y el
  outbox se leen/escriben a través de `mod-checkout.public`.

## Capacidades que registra

| Capacidad | Consumidor | Motivo |
|---|---|---|
| `order_confirmation` | worker de outbox (tras `nuevoEstado: PAGADO` de G5) | el correo de confirmación pertenece a este módulo |
| `feedback_notifier` | worker de outbox | correo interno de sugerencias/bugs (SPEC-24), mismo `EmailSender` |

## Retroalimentación (SPEC-24)

- `POST /sugerencias` (en `mod-chat`) encola `SEND_FEEDBACK_EMAIL`; el worker lo
  entrega a la bandeja `FEEDBACK_INBOX_EMAIL` con el `EmailSender` de SPEC-16.
- El detalle de pedido incluye `csatEnviado` leído de `order_ref.csat_sent`
  (solo si existe la fila local), para que el frontend no repita la encuesta.

## Pruebas

- Unitarias: `tests/unit/mod_orders/`
- Integración: `tests/integration/mod_orders/`
- Contrato: endpoint ↔ `docs/openapi.json` (tag `mod-orders`)

## Reglas

- No importar `domain/`, `application/` ni `adapters/` de otro módulo.
- No tocar `shared/` sin label `cross-modulo`.
- Un PR de este módulo no modifica archivos de otro `mod-*`.
