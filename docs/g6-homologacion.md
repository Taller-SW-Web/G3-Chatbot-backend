# Homologación con Productos (G6) en `MODULES_MODE=http`

Guía para correr el backend contra el entorno real de Productos y Ofertas,
separada de las pruebas con adaptadores falsos. Implementa el acuerdo **A17**
(cierre de compatibilidad del 20/10) y la decisión **D3** de
`contratos-integracion.md`: la homologación se hace con el modo HTTP y sus
resultados no se mezclan con los del modo `fake`.

> **Los resultados del modo `fake` no son evidencia de inventario, precio ni
> promociones reales.** Los adaptadores falsos comparten semillas, no el estado
> de G6.

## 1. Variables

Parte del perfil real de `.env.template` y aplica estos valores:

| Variable | Valor para homologar | Nota |
|---|---|---|
| `PRODUCTS_MODE` | `http` | Solo Productos/Inventario; el resto puede seguir en `fake`. |
| `PRODUCTS_BASE_URL` | URL del API de G6 (`.../api/v1`) | La define Mathias/Productos; no se inventa. |
| `PRODUCTS_AUTH_MODE` | `service` | A17 exige token de servicio. `public` es un respaldo temporal y **no** cumple A17. |
| `SECURITY_MODE` | `http` | El token se pide a Seguridad (G7). |
| `SECURITY_BASE_URL` | URL de G7 (`.../api/v1`) | La del perfil real. |
| `SECURITY_EXPECTED_ISSUER` | issuer de G7 | Evita aceptar tokens de otro emisor. |
| `SECURITY_CLIENT_ID` | `modulo-chatbot` | Identidad del canal. |
| `SECURITY_CLIENT_SECRET` | secreto del vault | **Nunca** se escribe en el repo. |
| `PRODUCTS_TIMEOUT_SECONDS` | `10` | G6 puede arrancar en frío. `INVENTORY_TIMEOUT_SECONDS` sigue en 3 s por SPEC-10. |
| `SALES_MODE` | `fake` | Ventas (G5) aún no está desplegado para el chatbot. |
| `DISPATCH_MODE` | `fake` o `http` | Independiente de esta homologación. |

`PRODUCTS_AUTH_MODE=service` sin `SECURITY_CLIENT_SECRET` falla de forma
explícita (`SECURITY_CLIENT_SECRET required for G6 M2M`): no hay respaldo
silencioso a `public` ni a `fake`.

## 2. Qué envía el chatbot en cada llamada

Con el modo `service`, `ProductsClient` e `InventoryClient` piden a Seguridad un
token `client_credentials` con:

- **audiencia** `api-productos`;
- **scope de la operación**:

| Operación | Scope |
|---|---|
| `GET /productos`, `GET /productos/{id}`, `GET /categorias`, `GET /marcas` | `catalogo:leer` |
| `GET /precios`, `GET /precios/skus/{sku}` | `precios:leer` |
| `GET /promociones` | `promociones:leer` |
| `POST /promociones/evaluar` | `promociones:evaluar` |
| `GET /recomendaciones` | `recomendaciones:leer` |
| `GET /inventario/disponibilidad/comercial` y `POST .../verificar` | `inventario:disponibilidad:leer` |

Cada request lleva además:

- `Authorization: Bearer <token>`;
- `X-Correlation-Id` en formato UUID, el mismo durante todo el turno;
- `canal=CHATBOT` donde el contrato lo exige (`/productos`, detalle, precios,
  promociones, recomendaciones, inventario);
- **nunca** `estado` (es filtro de backoffice).

La verificación por cantidad envía
`{channel_id: "CHATBOT", fulfillment: {mode: "ENVIO_DOMICILIO"}, lines[{sku, quantity}]}`,
sin `fulfillment_ref`, y lee `items[].result` (`SUFICIENTE`, `INSUFICIENTE`,
`INDETERMINADO`).

## 3. Cómo levantar el backend

```bash
# 1. .env con el perfil real y las variables de la tabla
cp .env.template .env

# 2. Dependencias y esquema
pip install -r requirements.txt
alembic upgrade head

# 3. Servidor
uvicorn src.infrastructure.main:app --reload --env-file .env
```

Comprobaciones mínimas antes de dar una corrida por válida:

1. `GET /healthz` responde 200.
2. `GET /api/v1/catalogo/productos?q=zapatillas` devuelve `items` con
   `product_id` reales (no `prod-001` de las semillas).
3. En los logs de G6/Seguridad aparece `POST /auth/token` con `audience=api-productos`
   y el scope esperado.
4. Un `sku` real responde precio y disponibilidad; un `sku` inexistente
   devuelve `missing_data: ["availability"]`, no `AGOTADO`.
5. `X-Correlation-Id` de la misma petición coincide en todas las llamadas a
   Productos.

## 4. Qué queda pendiente fuera del backend

Estos puntos no se pueden cerrar desde este repositorio y bloquean la
homologación formal (no la implementación):

- **Entorno de G6** (local o staging): URL, versión, SHA y evidencia. Lo
  define Mathias con Productos.
- **Seguridad (G7)**: registro y concesión de los siete scopes a
  `modulo-chatbot` y entrega de credenciales (`SECURITY_CLIENT_SECRET`).
- **Matriz de errores y límites B9-B10** de Productos.

Hasta que existan, la evidencia válida es la de los tests con `respx` contra el
OpenAPI publicado (`tests/unit/test_a17_productos_alignment.py`), no una
corrida real.
