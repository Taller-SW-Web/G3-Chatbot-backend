# Consistencia de totales entre carrito y checkout

- **Fecha:** 2026-11-25
- **Alcance:** `mod-cart`, `mod-checkout`
- **Specs:** SPEC-11 Req. 3, SPEC-13 Req. 4, SPEC-14 Req. 2
- **Contrato HTTP:** sin cambios de rutas ni códigos; `409 CARRITO_DESACTUALIZADO`
  ahora puede incluir `motivo`. El `503 MODULO_NO_DISPONIBLE` del checkout puede
  incluir `checkoutId`, `orderId`, `orderIdProvisional` y `reintentable`.

## El incidente

En la prueba de integración del 2026-11-25, el pedido `PED-2026-00088` se creó
por **S/ 239.00** aunque el carrito mostraba **S/ 179.90** (descuento de
**S/ 49.10**).

Secuencia observada:

1. `POST /carrito/items` mostró `promociones_ok: false` y persistió un snapshot
   sin descuento.
2. `GET /carrito` recalculó el descuento y lo mostró, pero el `UPDATE` de
   `cart.totals_snapshot` no se confirmó: un `GET` no tenía punto de commit.
3. `POST /checkout` leyó el snapshot anterior (sin descuento); como el subtotal
   coincidía, aplicó `descuento: 0` y creó el pedido.

La causa no era G6: era un snapshot desactualizado tratado como si fuera una
evaluación válida con descuento cero.

## Invariante

El checkout solo crea un pedido cuando el snapshot del carrito prueba que:

1. La evaluación de promociones se completó (`promociones_ok: true`).
2. El subtotal del snapshot es el de las líneas actuales del carrito.
3. El total del snapshot es `subtotal - descuentos` (el envío se cotiza aparte).

Si no se cumple, responde `409 CARRITO_DESACTUALIZADO` con `motivo` y `summary`
recalculado por el servidor. **No** se crea checkout ni pedido.

## Flujo correcto

1. `GET /carrito` (o cualquier mutación) recalcula precios y promociones,
   actualiza `cart_item.unit_price_ref` al último precio visto y persiste
   `cart.totals_snapshot`. El `GET` hace `commit` de ese snapshot.
2. `POST /checkout` revalida stock, cotiza el envío y valida el snapshot.
3. Si el snapshot no es confiable, el checkout **recalcula el carrito una vez**
   (la misma ruta que `GET /carrito`), persiste el snapshot fresco y responde
   `409` con ese resumen. El reintento del cliente ya usa el snapshot nuevo, sin
   tener que volver al carrito.
4. Si el snapshot es confiable → crea checkout y pedido con el descuento del
   snapshot.

El `409` tiene un efecto lateral acotado: refresca el snapshot del carrito y
los precios vistos. **No** crea checkout ni pedido.

## Motivos del `409 CARRITO_DESACTUALIZADO`

| `motivo` | Cuándo | Qué debe hacer el cliente |
|---|---|---|
| `SIN_EVALUACION` | No hay snapshot, o no tiene `subtotal`/`descuentos` válidos | Confirmar el resumen devuelto; el backend ya persistió uno nuevo |
| `PROMOCIONES_NO_DISPONIBLES` | `promociones_ok` no es `true` (G6 falló o snapshot previo al deploy) | Reintentar cuando G6 responda; el resumen devuelto es el vigente |
| `SUBTOTAL_CAMBIADO` | El subtotal del snapshot no coincide con las líneas actuales | Revisar el nuevo resumen y confirmar |
| `TOTAL_INCONSISTENTE` | El total del snapshot no cuadra con `subtotal - descuentos` | Revisar el nuevo resumen y confirmar |

El `summary` del error siempre lo calcula el servidor: líneas, subtotal,
`descuento: 0`, costo de envío cotizado y total. Nunca se confía en importes del
payload del cliente.

## Timeout de Ventas: checkout y pedido reconciliables (issue #180)

El 2026-11-26 el primer `POST /checkout` superó los 120 s por el cold start de
G5. El cliente perdió la respuesta, pero Ventas ya había creado
`PED-2026-00090`: un pedido sin checkout local visible. La corrección tiene dos
partes.

### 1. Deadline interno

`CHECKOUT_SALES_DEADLINE_SECONDS` (default **30 s**) limita la llamada
`POST /pedidos` a Ventas. El cliente/proxy del frontend corta a los 120 s
(incidente 2026-11-26) y el cold start de G5 puede superar ese tiempo; 30 s deja
margen para stock, envío, promociones y la respuesta del BFF. El valor es
configurable por entorno; si G5 sigue en cold start, el primer intento responde
rápido y el reintento encuentra el servicio caliente.

### 2. Checkout persistido antes de llamar a Ventas

`POST /checkout` guarda y **commitea** el checkout en `PENDING_PAYMENT` antes de
`POST /pedidos`. El `summary` guarda un `orderId` local provisional
(`PED-<año>-<hex>`) y, en claves internas que no se exponen por HTTP, el
snapshot exacto enviado a Ventas y la marca de pedido pendiente.

Si la llamada termina a tiempo, el checkout se actualiza con el `pedidoId` real
y se registra `order_ref` en la misma transacción.

Si G5 no responde dentro del deadline (o falla la conexión/5xx), el BFF responde
un `503` recuperable; el checkout local ya existe y no se pierde:

```json
{
  "status": 503,
  "code": "MODULO_NO_DISPONIBLE",
  "title": "Service temporarily unavailable",
  "modulo": "sales",
  "motivo": "TIMEOUT_VENTAS",
  "checkoutId": "a93cfced-e936-416a-94f7-222a2739fe6f",
  "orderId": "PED-2026-A1B2C3",
  "orderIdProvisional": true,
  "reintentable": true,
  "detail": "No pudimos confirmar tu pedido con Ventas. No se realizó ningún cobro; reintenta con la misma Idempotency-Key o consulta el checkout."
}
```

- `checkoutId` permite `GET /checkout/{id}` de inmediato.
- `orderId` es el id local provisional mientras `orderIdProvisional` sea `true`;
  no debe mostrarse como pedido confirmado.
- `motivo` es `TIMEOUT_VENTAS` si venció el deadline y
  `VENTAS_NO_DISPONIBLE` para otros fallos de transporte/5xx.

### Reintento con la misma `Idempotency-Key`

Un reintento con la misma clave no crea otro checkout ni otro pedido:

1. El BFF encuentra el checkout local por `idempotency_key`.
2. Reenvía a Ventas el **mismo snapshot persistido** con la misma clave.
3. Ventas responde con el `pedidoId` que ya había creado (idempotencia de G5).
4. El BFF actualiza `summary.orderId` al id real, guarda `order_ref` y responde
   `201` con el checkout reconciliado.

El snapshot persistido evita que un reintento con dirección/contacto distintos
envíe otro payload bajo la misma clave.

### Pago mientras el pedido está provisional

Mientras `summary` tenga la marca interna de pedido pendiente, `POST
/checkout/{id}/pago` responde `503 MODULO_NO_DISPONIBLE` con
`motivo: PEDIDO_EN_PROCESO`, `checkoutId`, `orderId` y `reintentable: true`.
Nunca se cobra contra un `pedidoId` que todavía no existe en Ventas. El cliente
debe reintentar primero el checkout con la misma clave.

### Fallos definitivos de Ventas (400/409)

Un `409` o `400` de Ventas significa que **no** se creó el pedido. El BFF retira
el checkout provisional:

- lo marca `EXPIRED` (deja de ser un checkout vivo y libera el índice único del
  carrito);
- devuelve el carrito a `ACTIVE`;
- conserva el error de contrato: `409 STOCK_INSUFICIENTE` o
  `400 DATO_INVALIDO`.

El siguiente intento con una clave nueva puede crear un checkout limpio; ningún
checkout vivo queda bloqueando el carrito.

### Expiración de un pedido provisional

Si el cliente nunca reintenta, el job `ExpirarCheckouts` (o el `GET` que detecta
la expiración) reconcilia el pedido con la misma clave antes de cancelarlo. La
anulación `REQUEST_CANCELLATION` usa el `pedidoId` real, nunca el provisional.
Si Ventas no responde en ese momento, se registra un `ERROR` y la expiración
propia de G5 (15 min) termina liberando la reserva; el checkout local ya está
`EXPIRED`.

## Snapshot persistido

`cart.totals_snapshot` (JSONB) guarda la última vista del carrito:

```json
{
  "subtotal": "229.00",
  "descuentos": [{"name": "Descuento", "amount": "49.10"}],
  "total": "179.90",
  "promociones_ok": true,
  "promotion_reason": null
}
```

- `promociones_ok: false` **no** equivale a un descuento de cero.
- `unit_price_ref` es el último precio visto y se refresca en cada lectura,
  como documenta `modelo-datos`.
- El snapshot es interno: no se expone en el contrato HTTP.

## Verificación

```bash
# Unitarios del carrito y checkout
python -m pytest tests/unit/mod_cart tests/unit/mod_checkout -q

# Integración PostgreSQL (requiere TEST_DATABASE_URL)
python -m pytest tests/integration/mod_checkout -q
```

Regresiones cubiertas:

- `test_view_commits_snapshot_and_last_seen_price`
- `test_view_persists_unavailable_promotions_instead_of_zero_discount`
- `test_create_checkout_without_cart_evaluation_is_409`
- `test_create_checkout_with_unavailable_promotions_is_409`
- `test_create_checkout_with_legacy_snapshot_without_flag_is_409`
- `test_create_checkout_with_inconsistent_snapshot_total_is_409`
- `test_create_checkout_stale_snapshot_raises_409_with_server_summary`
- `test_sales_timeout_persists_checkout_and_returns_recoverable_ids`
- `test_same_key_retry_reconciles_the_order_without_duplicates`
- `test_new_key_retry_reuses_the_pending_checkout_without_duplicates`
- `test_replaced_pending_checkout_cancels_its_sales_order`
- `test_happy_path_keeps_one_sales_call_and_real_order_id`
- `test_definitive_sales_rejection_retires_checkout_and_frees_cart`
- `test_payment_is_blocked_while_the_sales_order_is_provisional`
- `test_expiry_reconciles_the_pending_order_and_cancels_the_real_id`
- `test_post_checkout_sales_timeout_includes_recoverable_ids`
- `test_get_checkout_hides_internal_sales_reconciliation_keys`

## Riesgos y notas

- Un carrito creado antes de este cambio no tiene `promociones_ok`; el primer
  `POST /checkout` recalcula el carrito, lo persiste y responde `409`. El
  reintento ya usa el snapshot nuevo.
- Si G6 está caído, el carrito se puede ver y modificar, pero el checkout queda
  bloqueado con `409` en lugar de cobrar sin descuento. Es intencional.
- El refresco de `unit_price_ref` evita un `409` permanente cuando un precio
  cambia entre agregar y pagar: la vista y el checkout comparan el mismo precio.
- El recálculo del `409` commitea el snapshot y cualquier cambio de estado
  previo del checkout en la misma sesión (por ejemplo, expirar un checkout
  pendiente). Nunca guarda un checkout ni crea un pedido.
