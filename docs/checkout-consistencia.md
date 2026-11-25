# Consistencia de totales entre carrito y checkout

- **Fecha:** 2026-11-25
- **Alcance:** `mod-cart`, `mod-checkout`
- **Specs:** SPEC-11 Req. 3, SPEC-13 Req. 4, SPEC-14 Req. 2
- **Contrato HTTP:** sin cambios de rutas ni códigos; `409 CARRITO_DESACTUALIZADO`
  ahora puede incluir `motivo`.

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
