# Contrato G6 — Productos y Ofertas

Fixture: `fixtures/g6_openapi.yaml`, copia byte a byte de
`Taller-SW-Web/G6-Productos-Ofertas-docs` · `contratos/http/openapi.yaml` en
`f785accbc7c290ba10f873c8f12061405bb2c622`, versión **0.7.0-rc.3**.

Cubre las operaciones que usa `mod_catalog`:

- `GET /productos` (búsqueda con `canal`, paginación y filtros de precio).
- `GET /productos/{productoId}` (detalle con `marca` y `variants`).
- `GET /precios` y `GET /precios/skus/{sku}`.
- `GET /promociones` y `POST /promociones/evaluar`.
- `GET /inventario/disponibilidad/comercial` y
  `POST /inventario/disponibilidad/comercial/verificar`.

Los clientes se prueban con el `base_url` de despliegue, que incluye `/api/v1`;
los paths del cliente no repiten ese prefijo. Los JSON de `fixtures/` son
payloads conformes al OpenAPI, no respuestas capturadas de producción.

El lock de plataforma (`tests/contract/contracts.lock.json`) fija el hash y la
versión; al actualizar el contrato se reemplaza el YAML y se ejecuta
`python scripts/check_contract_drift.py --update` desde el PR de plataforma.
