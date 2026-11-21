# `mod-checkout` — Checkout y pago

- **Owner:** @MathiasTL
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

## Dependencias externas

- **G1 Seguridad** (perfil, prefill de contacto) con el token del titular.
- **G2 Despacho** (cotización) por `mod-shipping.public`.
- **G5 Ventas** `POST /pedidos`, `POST /pedidos/{id}/pagos/notificacion`,
  `POST /pedidos/{id}/anulaciones` (`provide_sales_port`).
- **G7 correo** (confirmación) a través de la capacidad `order_confirmation`
  que registra `mod-orders`; el outbox vive en `src/shared`.

## Celular verificado (SPEC-04)

`POST /checkout` exige que el celular vigente de `GET /auth/me` tenga una
verificación local exacta (`local_phone_verification`). Con
`REQUIRE_PHONE_VERIFICATION` activo, una falta de verificación responde
`403 CELULAR_NO_VERIFICADO` con el número enmascarado; si Seguridad no responde,
el checkout se bloquea con el mismo código y `motivo=SEGURIDAD_NO_DISPONIBLE`.
En desarrollo/fake el flag está apagado por defecto para no romper los E2E.

## Stock (SPEC-10)

Antes de cotizar y crear el pedido, `create_checkout` invoca
`ManageCartUseCase.validate_cart` (una consulta masiva de disponibilidad y una
verificación por cantidad). Si alguna línea está `AGOTADO`/`INSUFICIENTE` o no
se pudo confirmar, responde `409 CARRITO_DESACTUALIZADO` con
`lineas[{sku, nombre, cantidad, motivo, mensaje}]`; nada se degrada en silencio.
`STOCK_BAJO` e `INDETERMINADO` no bloquean (la decisión final sigue en Ventas).

## Capacidades que registra

| Capacidad | Consumidor | Motivo |
|---|---|---|
| `checkout_document` | `mod-auth` (opcional) | `ultimoDocumento` del perfil sin acoplar auth a checkout |
| `outbox_processor` | worker compartido | el lifespan no importa módulos |
| `order_confirmation` (consumida) | este módulo | el correo pertenece a `mod-orders`; es requisito de arranque |

## Pruebas

- Unitarias: `tests/unit/mod_checkout/`
- Integración: `tests/integration/mod_checkout/`
- Contrato: endpoint ↔ `docs/openapi.json` (tag `mod-checkout`)

## Reglas

- No importar `domain/`, `application/` ni `adapters/` de otro módulo.
- No tocar `shared/` sin label `cross-modulo`.
- Un PR de este módulo no modifica archivos de otro `mod-*`.
