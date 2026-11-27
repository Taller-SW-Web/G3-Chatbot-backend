# Contrato G2 — Despacho y Entrega

No existe un OpenAPI machine-readable de G2. La fuente es el Markdown
`Taller-SW-Web/G2-Despacho-Specs` · `integraciones/api-contract.md` en
`2bd99a23df22329d42ca9806b4debe9adcc05999`.

- `fixtures/g2_api_contract.md` es la copia byte a byte del documento completo.
- Los JSON de `fixtures/` transcriben a mano los ejemplos de:
  - §5.1 `POST /api/v1/cotizaciones` (cotización exacta y solo cobertura),
  - §6.1 `GET /api/v1/seguimientos/pedidos/{idPedido}` (seguimiento con hitos),
  - §3.5 formato de error RFC 7807 (`code`, `detail`, `correlationId`, etc.).
- Esta limitación está anotada también en `tests/contract/README.md`.

Los paths se prueban con el `base_url` de despliegue (`DISPATCH_BASE_URL`
incluye `/api/v1`); el cliente omite el prefijo en su código, igual que en el
resto de módulos.

## Seguimiento

El test valida el **payload crudo del cliente**: `estado`, `estadoEtiqueta` y
`hitos[].etiqueta`/`hitos[].estado`. No se prueba el mapper de `mod_orders`
(`src/modules/mod_orders/domain/tracking_mapper.py`): su alineación vive en
#213/#82 y no forma parte del contrato del cliente de Despacho.

## Solo cobertura

El contrato permite omitir `lineas` para consultar solo cobertura. El BFF de
este repositorio siempre cotiza con las líneas del carrito, así que esa variante
no se ejercita aquí; `g2_cotizacion_cobertura.json` queda como referencia de la
forma documentada. No se envía `lineas: []` porque el contrato exige que, si se
envía, tenga al menos un elemento.
