# Contrato G5 — Postventa (M2)

Fixture: `fixtures/postventa.yaml`, copia byte a byte de
`Taller-SW-Web/G5-Ventas-Postventas-Backend` ·
`servicio-postventa/src/main/resources/static/openapi/postventa.yaml` en
`41516ef5f9812d5a43f7380ec06cba5fee79ea90`, versión **1.6.0**.
También se publica en
`https://g5-ventas-postventas-backend.onrender.com/openapi/postventa.yaml`.

Cubre con `PostventaClient` + `respx`:

- `POST /api/v2/devoluciones/evidencias/upload` (multipart, campo `file`).
- `POST /api/v2/devoluciones`, `GET /api/v2/devoluciones/me` y
  `GET /api/v2/devoluciones/{devolucionId}`.
- `POST /api/v2/reclamos`, `GET /api/v2/reclamos/me` y
  `GET /api/v2/reclamos/{codigoSeguimiento}`.
- `POST /api/v2/csat` con `201` sin cuerpo (el cliente devuelve `{}`).

`pagina` es **desde 0** en este contrato; el cliente convierte `page=1` a
`pagina=0`. Las operaciones usan el Bearer del cliente final (acuerdo
2026-11-17), no un token de servicio.

## CSAT 403/404 (issue #84)

La issue #84 se cerró con la decisión `404 RECURSO_NO_ENCONTRADO`. Los tests
cubren el `201` sin cuerpo, el `403 ACCESO_DENEGADO` y el
`404 PEDIDO_NO_ENCONTRADO` de `POST /api/v2/csat`. El `409 CSAT_ALREADY_SUBMITTED`
y el `422 ORDER_NOT_DELIVERED` siguen fuera de este PR.

El lock de plataforma fija hash/versión; al actualizar el YAML se ejecuta
`python scripts/check_contract_drift.py --update` desde el PR de plataforma.
