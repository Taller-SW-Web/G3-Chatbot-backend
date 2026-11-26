# `mod-checkout` — Checkout y pago

- **Owner:** @Taller-SW-Web/chatbot
- **Backend:** paquete `mod_checkout`, `basePath` `/checkout`
- **Frontend:** carpeta `mod-checkout`, rutas `[/checkout]`, `[/exito]`
- **Depende de:** `mod-cart` y `mod-shipping` (solo por `public.py`)
- **Contrato:** `docs/openapi.json`, tag `mod-checkout`.

## Responsabilidad

Crear el checkout desde el carrito, cotizar el envío, procesar el pago simulado,
crear el pedido en Ventas y encolar los eventos del outbox transaccional
(`NOTIFY_SALES_PAYMENT`, `REQUEST_CANCELLATION`, `SEND_EMAIL`). **No** lista
pedidos ni gestiona postventa.

## Superficie pública

| Tipo | Archivo | Qué expone |
|---|---|---|
| Backend | `public.py` | `provide_checkout_service`, `provide_sales_port`, `provide_checkout_snapshot`, `provide_confirmation_outbox`, `provide_outbox_processor`, `ManageCheckoutUseCase`, `PaymentGatewayPort` |
| Frontend | `public.ts` | Tipos de checkout, pago y resultado |

## Rutas

| Método | Ruta | Caso de uso |
|---|---|---|
| POST | `/api/v1/checkout` | `ManageCheckoutUseCase.create_checkout` |
| GET | `/api/v1/checkout/{checkout_id}` | `ManageCheckoutUseCase.get_checkout` |
| POST | `/api/v1/checkout/{checkout_id}/pago` | `ManageCheckoutUseCase.process_payment` |
| POST | `/api/v1/pedidos/{pedido_id}/valoracion-compra` | `PurchaseRatingService.create` |

## Dependencias externas

- **G1 Seguridad** (perfil, prefill de contacto) con el token del titular.
- **G2 Despacho** (cotización) por `mod-shipping.public`.
- **G5 Ventas** `POST /pedidos`, `POST /pedidos/{id}/pagos/notificacion`,
  `POST /pedidos/{id}/anulaciones` (`provide_sales_port`).
- **G7 correo** (confirmación) a través de la capacidad `order_confirmation`
  que registra `mod-orders`; el outbox vive en `src/shared`.

## Contacto verificado (SPEC-04)

`POST /checkout` exige un contacto verificado. Por defecto el correo del perfil
de `GET /auth/me` (verificado al crear la cuenta en G7) es el contacto válido y
se normaliza en `summary.contacto.email`; el `email` del payload se ignora. Un
celular verificado localmente (`local_phone_verification`) se acepta como
respaldo cuando el perfil no trae correo. Sin correo en el perfil y sin celular
verificado responde `403 CONTACTO_NO_VERIFICADO`
(`motivo=SIN_CONTACTO_VERIFICADO`), con el número enmascarado si hay celular. Si
Seguridad no responde (`SEGURIDAD_NO_DISPONIBLE`) o rechaza el token
(`SIN_SESION`), el checkout se bloquea con el mismo código.

`REQUIRE_VERIFIED_CONTACT` (default `true` en producción, `false` en
desarrollo/fake) activa este guard. El modo legado
`REQUIRE_PHONE_VERIFICATION=true` (solo celular, `403 CELULAR_NO_VERIFICADO`)
tiene precedencia cuando ambos están activos.

## Stock (SPEC-10)

Antes de cotizar y crear el pedido, `create_checkout` invoca
`ManageCartUseCase.validate_cart` (una consulta masiva de disponibilidad y una
verificación por cantidad). Si alguna línea está `AGOTADO`/`INSUFICIENTE` o no
se pudo confirmar, responde `409 CARRITO_DESACTUALIZADO` con
`lineas[{sku, nombre, cantidad, motivo, mensaje}]`; nada se degrada en silencio.
`STOCK_BAJO` e `INDETERMINADO` no bloquean (la decisión final sigue en Ventas).
Un fallo del validador deja un `WARNING` con `module`, `status`, `code` y
`correlation_id`, y la respuesta `503 MODULO_NO_DISPONIBLE` conserva el
`X-Correlation-Id` de la petición.

## Totales y promociones (SPEC-14 Req. 2)

Antes de crear el checkout, `create_checkout` exige que `cart.totals_snapshot`
demuestre una evaluación de promociones exitosa para las líneas actuales. Si
falta el snapshot, `promociones_ok` no es `true`, el subtotal no coincide o el
total no cuadra con los descuentos, responde `409 CARRITO_DESACTUALIZADO` con
`motivo` (`SIN_EVALUACION`, `PROMOCIONES_NO_DISPONIBLES`, `SUBTOTAL_CAMBIADO` o
`TOTAL_INCONSISTENTE`) y un `summary` recalculado por el servidor. Antes de
responder, el checkout recalcula el carrito (misma ruta que `GET /carrito`) y
persiste el snapshot fresco, de modo que el reintento del cliente usa el resumen
que acaba de ver. **No** se crea checkout ni pedido con un total distinto al del
carrito.

El descuento nunca se toma del payload del cliente. Detalle en
[`docs/checkout-consistencia.md`](../../../docs/checkout-consistencia.md).

## Pedido en Ventas y timeout (SPEC-15, issue #180)

El checkout se commitea localmente antes de `POST /pedidos`. La llamada a Ventas
tiene un deadline interno (`CHECKOUT_SALES_DEADLINE_SECONDS`, default 30 s): si
vence, el BFF responde `503 MODULO_NO_DISPONIBLE` con `motivo=TIMEOUT_VENTAS`,
`checkoutId`, `orderId` provisional y `reintentable=true`. Un reintento con la
misma `Idempotency-Key` reenvía el snapshot persistido y reconcilia el
`pedidoId` real sin duplicar checkout ni pedido. Un `400/409` definitivo retira
el checkout (`EXPIRED`) y libera el carrito. Detalle en
[`docs/checkout-consistencia.md`](../../../docs/checkout-consistencia.md).

## Dirección guardada (SPEC-12)

`POST /checkout` acepta `guardarDireccion: true`. Con el cliente autenticado se
llama a `POST /usuarios/{id}/direcciones` de Seguridad con su token; si falla,
el checkout continúa y la respuesta incluye `avisoDireccion` ("No pudimos
guardar tu dirección para la próxima vez"). `CHECKOUT_SAVE_ADDRESS_ENABLED`
(default true) permite apagar la llamada sin tocar código.

## Valoración de compra (SPEC-24)

`POST /pedidos/{id}/valoracion-compra` guarda una única valoración por pedido
(estrellas 1–5 y comentario opcional) en `purchase_rating`, previa validación
de propiedad en Ventas. No se envía a Ventas; la CSAT de la entrega sigue en
`mod-postventa`.

## Capacidades que registra

| Capacidad | Consumidor | Motivo |
|---|---|---|
| `checkout_document` | `mod-auth` (opcional) | `ultimoDocumento` del perfil sin acoplar auth a checkout |
| `outbox_processor` | worker compartido | el lifespan no importa módulos |
| `order_confirmation` (consumida) | este módulo | el correo pertenece a `mod-orders`; es requisito de arranque |
| `feedback_notifier` (consumida) | worker de outbox | correo interno de SPEC-24; opcional, si falta el job reintenta |
| `outbox_publisher` (consumida) | este módulo | solo con `OUTBOX_DISPATCH_MODE=broker` (ADR-0030): el lifespan registra la conexión única a RabbitMQ y el procesador publica en lugar de despachar |

## Pruebas

- Unitarias: `tests/unit/mod_checkout/`
- Integración: `tests/integration/mod_checkout/`
- Contrato: endpoint ↔ `docs/openapi.json` (tag `mod-checkout`)

## Reglas

- No importar `domain/`, `application/` ni `adapters/` de otro módulo.
- No tocar `shared/` sin label `cross-modulo`.
- Un PR de este módulo no modifica archivos de otro `mod-*`.
