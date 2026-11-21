# `mod-cart` — Carrito

- **Owner:** @SOoOnny
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

`provide_cart_repositories` es lo que usa `mod-checkout` (dependencia declarada).
`mod-shipping` no puede importar este paquete (su `dependsOn` está vacío), así
que `configure()` registra la misma función como capacidad
`cart_repositories` y shipping la resuelve en tiempo de request.

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
- Memoria de conversación y mensajes: adaptadores compartidos de `src/shared`.

## Pruebas

- Unitarias: `tests/unit/mod_cart/`
- Integración: `tests/integration/mod_cart/`
- Contrato: endpoint ↔ `docs/openapi.json` (tag `mod-cart`)

## Reglas

- No importar `domain/`, `application/` ni `adapters/` de otro módulo.
- No tocar `shared/` sin label `cross-modulo`.
- Un PR de este módulo no modifica archivos de otro `mod-*`.
