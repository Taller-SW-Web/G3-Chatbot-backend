# `mod-cart` — Carrito

- **Owner:** @Taller-SW-Web/chatbot
- **Backend:** paquete `mod_cart`, `basePath` `/carrito`
- **Frontend:** carpeta `mod-cart`, ruta `[/carrito]`
- **Depende de:** `mod-catalog` (solo por `public.py`)
- **Contrato:** `docs/openapi.json`, tag `mod-cart`.

## Responsabilidad

Ciclo de vida del carrito anónimo (por conversación) y del cliente, ítems,
cupón y totales con stock en vivo. **No** cotiza envío (eso es `mod-shipping`)
ni crea pedidos (eso es `mod-checkout`).

## Superficie pública

| Tipo | Archivo | Qué expone |
|---|---|---|
| Backend | `public.py` | `provide_cart_service`, `provide_cart_repositories` (`CartRepositories`), `ManageCartUseCase`, `CartError` |
| Frontend | `public.ts` | Tipos de carrito e ítem |

`provide_cart_repositories` lo consumen `mod-checkout` y `mod-shipping`, ambos
con la dependencia declarada en `modules.manifest.json`. No hay capacidad oculta:
el import pasa por esta superficie pública y el checker de fronteras lo verifica.

## Rutas

| Método | Ruta | Caso de uso |
|---|---|---|
| GET | `/api/v1/carrito` | `ManageCartUseCase.view` |
| POST | `/api/v1/carrito/items` | `ManageCartUseCase.add_item` |
| PATCH | `/api/v1/carrito/items/{item_id}` | `ManageCartUseCase.change_quantity` |
| DELETE | `/api/v1/carrito/items/{item_id}` | `ManageCartUseCase.remove_item` |
| DELETE | `/api/v1/carrito` | `ManageCartUseCase.clear` (requiere `confirmado: true`) |
| POST | `/api/v1/carrito/cupon` | `ManageCartUseCase.apply_coupon` |
| DELETE | `/api/v1/carrito/cupon` | `ManageCartUseCase.remove_coupon` |

## Dependencias externas

- **G6 Productos** a través de los puertos que expone `mod-catalog`
  (`ProductCatalogPort`, `InventoryPort`, evaluación de promociones).
- Memoria de conversación y auditoría TOOL: `ConversationContextPort` de
  `src/shared/application/ports`, implementado por `mod-chat` y resuelto por la
  capacidad `conversation_context` (el DAG no permite importar chat).

## Totales y promociones (SPEC-11 Req. 3)

`_cart_view` es la única fuente de los totales que ve el cliente. Cada vez que
se muestra el carrito:

- recalcula precios con G6 y actualiza `cart_item.unit_price_ref` al último
  precio visto (la línea conserva el aviso de cambio vía `previous_price`);
- evalúa promociones una sola vez y persiste en `cart.totals_snapshot`
  `subtotal`, `descuentos`, `total`, `promociones_ok` y `promotion_reason`;
- `GET /carrito` hace commit de ese snapshot. Sin ese commit, el checkout
  podría leer un snapshot anterior (caso `PED-2026-00088`).

`promociones_ok: false` significa que G6 no pudo evaluar; **no** es un descuento
cero. El checkout lo trata como carrito desactualizado y no crea el pedido.
`recalculate_totals(cart)` expone la misma ruta de recálculo para que
`mod-checkout` refresque y persista el snapshot antes de responder `409`, sin
depender de que el cliente vuelva a abrir el carrito.
Detalle en [`docs/checkout-consistencia.md`](../../../docs/checkout-consistencia.md).

## Pruebas

- Unitarias: `tests/unit/mod_cart/`
- Integración: `tests/integration/mod_cart/`
- Contrato: endpoint ↔ `docs/openapi.json` (tag `mod-cart`)

## Reglas

- No importar `domain/`, `application/` ni `adapters/` de otro módulo.
- No tocar `shared/` sin label `cross-modulo`.
- Un PR de este módulo no modifica archivos de otro `mod-*`.
