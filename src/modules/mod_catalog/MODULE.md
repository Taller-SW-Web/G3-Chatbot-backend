# `mod-catalog` — Catálogo

- **Owner:** @Taller-SW-Web/chatbot
- **Backend:** paquete `mod_catalog`, `basePath` `/catalogo`
- **Frontend:** carpeta `mod-catalog`, rutas `[/]` (tarjetas y bloques de catálogo)
- **Depende de:** nadie en el backend. Su contrato con Productos (G6) es HTTP,
  configurable por `PRODUCTS_MODE`.
- **Contrato:** `docs/openapi.json`, tag `mod-catalog`.

## Responsabilidad

Búsqueda, detalle, disponibilidad, promociones y precio proyectado del catálogo
de Productos, más las reglas puras de producto (variantes, ofertas, similitud,
stock). **No** incluye carrito, checkout ni conversación: la memoria del chat se
lee/escribe con `ConversationContextPort`, implementado por `mod-chat` y
resuelto por la capacidad `conversation_context`.

## Superficie pública

| Tipo | Archivo | Qué expone |
|---|---|---|
| Backend | `public.py` | `provide_product_catalog`, `provide_product_inventory`, `provide_catalog_service`, `ManageCatalogUseCase`, `build_tool_registry` y los esquemas de herramientas (`SearchArguments`, `ProductArguments`, `OrderQueryArguments`, `ClaimConfirmArguments`, `ReturnConfirmArguments`), mǭs las reglas de producto que `mod-cart` reutiliza (`VariantResolver`, `normalize_detail`, `sellable_skus`, `price_projection`, `StockValidator`) |
| Frontend | `public.ts` | Tipos de producto, tarjeta y disponibilidad |

Todo lo demás (`adapters/`, `application/discovery.py`, `application/partial.py`)
es privado.

## Rutas

| Método | Ruta | Caso de uso |
|---|---|---|
| GET | `/api/v1/catalogo/productos` | `ManageCatalogUseCase.search` |
| GET | `/api/v1/catalogo/promociones` | `ManageCatalogUseCase.promotions` |
| GET | `/api/v1/catalogo/productos/{product_id}` | `ManageCatalogUseCase.detail` |
| GET | `/api/v1/catalogo/disponibilidad` | `ManageCatalogUseCase.availability` |

`GET /catalogo/productos` acepta `pagina` con `1 <= pagina <= 1000`; fuera de
rango responde `400 VALIDACION` antes de consultar Productos (#255).

## Dependencias externas

- **G6 Productos** vía `ports/product_repository_port.py` (`ProductCatalogPort`,
  `InventoryPort`), `adapters/products_client.py` e `inventory_client.py`.
- Fixtures de demo en `adapters/seeds/` para `PRODUCTS_MODE=fake`.

## Errores de módulo y 503

Un fallo inesperado (por ejemplo `IntegrityError`) responde
`503 SERVICIO_NO_DISPONIBLE` con un `detail` genérico y `correlationId`; la
excepción y su traza quedan solo en el log interno. Un módulo caído conserva
`modulo` y `Retry-After`. Nunca se publica `type(exc).__name__` (#255).

## Orden de resultados (SPEC-06)

`GET /catalogo/productos` acepta `orden`: `relevancia` (default), `precio_asc`,
`precio_desc`, `nombre_asc`, `nombre_desc`. Productos solo ordena por nombre; el
BFF aplica el orden sobre la página devuelta (hasta 10) y deja los productos sin
precio al final. El orden es de página hasta que Productos publique precio
(A16); `NOVEDAD`/`RELEVANCIA` conservan el orden del proveedor.

## Paginación y resultados vacíos (SPEC-06 Req. 4)

`buscar_productos` agrega `total` y `siguiente` dentro de
`CARRUSEL_PRODUCTOS.datos`. `total` es el entero reportado por Productos (el
total consultado cuando la respuesta es parcial); `siguiente` es el payload de
`BUSCAR_PAGINA` con `pagina` y los filtros normalizados, o `null` en la última
página. `BUSCAR_PAGINA` con solo `pagina` reutiliza `filtrosVigentes`, de modo
que el frontend no reconstruye filtros. Sin resultados, la respuesta trae
`ACCIONES_RAPIDAS` con hasta 3 `BUSCAR_PRODUCTOS` que quitan un filtro activo
(precio máximo, marca, categoría, precio mínimo, talla, color, ofertas o texto
libre), usando solo marcas y categorías reales del catálogo.

## Pruebas

- Unitarias: `tests/unit/mod_catalog/`
- Integración: `tests/integration/mod_catalog/`
- Contrato: endpoint ↔ `docs/openapi.json` (tag `mod-catalog`)

## Reglas

- No importar `domain/`, `application/` ni `adapters/` de otro módulo.
- No tocar `shared/` sin label `cross-modulo`.
- Un PR de este módulo no modifica archivos de otro `mod-*`.
