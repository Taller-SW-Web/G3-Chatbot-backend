# Pruebas de contrato de módulos externos (H5-I1)

Este directorio fija la forma de los contratos de **G7 (Seguridad)**, **G6
(Productos y Ofertas)**, **G2 (Despacho)** y **G5 (Ventas y Postventa)** y
comprueba, con `respx`, que los clientes reales del backend leen exactamente esa
forma. No hay red ni base de datos: cada respuesta sale de un fixture versionado.

El job `contract` de `.github/workflows/ci.yml` ejecuta:

```powershell
python -m pytest tests/contract -q
ruff check tests/contract scripts
```

Es un job bloqueante del required check `test`. Un cambio de contrato upstream
que el cliente no sepa leer debe fallar aquí, no en producción.

## Fuentes fijadas

`contracts.lock.json` guarda repositorio, ref completo, ruta, versión, estado de
publicación y SHA-256 de cada contrato. El fixture local es una copia byte a byte
del archivo en ese ref.

| ID | Módulo | Repositorio | Ref | Ruta | Versión | SHA-256 |
|---|---|---|---|---|---|---|
| `g7-seguridad` | shared | `Taller-SW-Web/Modulo-de-Seguridad` | `fa93a88d03fd9fcd80b63edd025d82eb21234d98` | `specs/contrato-api/openapi.yaml` | `1.2.0` (revisión local) | `e8818bc40b47…` |
| `g6-productos` | mod_catalog | `Taller-SW-Web/G6-Productos-Ofertas-docs` | `f785accbc7c290ba10f873c8f12061405bb2c622` | `contratos/http/openapi.yaml` | `0.7.0-rc.3` | `e66fbbc2d683…` |
| `g2-despacho` | mod_shipping | `Taller-SW-Web/G2-Despacho-Specs` | `2bd99a23df22329d42ca9806b4debe9adcc05999` | `integraciones/api-contract.md` | sin OpenAPI | `7b267eebb933…` |
| `g5-ventas` | shared | `Taller-SW-Web/G5-Ventas-Postventas-Backend` | `41516ef5f9812d5a43f7380ec06cba5fee79ea90` | `servicio-ventas/src/main/resources/static/openapi/ventas.yaml` | `1.10.0` | `53f6bf94de1c…` |
| `g5-postventa` | mod_postventa | `Taller-SW-Web/G5-Ventas-Postventas-Backend` | `41516ef5f9812d5a43f7380ec06cba5fee79ea90` | `servicio-postventa/src/main/resources/static/openapi/postventa.yaml` | `1.6.0` | `974cc62629b2…` |

G5 también publica los contratos desplegados en
`https://g5-servicio-ventas.onrender.com/openapi/ventas.yaml` y
`https://g5-ventas-postventas-backend.onrender.com/openapi/postventa.yaml`.
El lock usa el archivo del repositorio en el ref porque es inmutable; las URLs
desplegadas se consultan a mano al revisar deriva.

## Estructura

```text
tests/contract/
├── contracts.lock.json          # pin de los cinco contratos
├── README.md
├── test_contract_drift.py       # pruebas del script de deriva y del lock
├── shared/                      # PR 1: G7 login/JWKS/introspección + G5 ventas
│   ├── fixtures/
│   │   ├── g7_openapi.yaml
│   │   ├── g7_*.json
│   │   ├── ventas.yaml
│   │   └── g5_*.json
│   └── test_contract_g7_*.py, test_contract_g5_ventas.py
├── mod_catalog/                 # PR 2: G6 productos, precios, promociones, inventario
├── mod_shipping/                # PR 3: G2 cotización y seguimiento
└── mod_postventa/               # PR 4: G5 postventa (evidencias, devoluciones, reclamos, CSAT)
```

Cada PR de módulo es independiente y se basa en `origin/development`; solo el
PR de plataforma agrega el job de CI y el lock. Hasta que ese PR mergee, los PRs
de módulo se validan a mano con `python -m pytest tests/contract/<módulo> -q`.

## Cómo actualizar un contrato

1. Confirmar el nuevo ref y traer el archivo upstream:

   ```powershell
   git -C <clon-del-repo> fetch origin
   git -C <clon-del-repo> show <ref>:<ruta> > tests/contract/<módulo>/fixtures/<archivo>
   ```

   Para G6, G5 y G7 el fixture es el OpenAPI completo. Para G2 es el Markdown
   completo (`g2_api_contract.md`); los payloads de ejemplo se extraen a mano.
2. Revisar el diff del fixture y ajustar los tests si el contrato cambió de
   forma. **No** se corrige `src/modules/**` ni `src/shared/**` en un PR de
   contrato: si el cliente quedó desalineado, se documenta y se abre una issue
   bloqueante en `G3-Chatbot-specs`.
3. Actualizar hashes/versiones/estado del lock:

   ```powershell
   python scripts/check_contract_drift.py --update
   ```

4. Actualizar la tabla de este README si cambió ref o versión.
5. Ejecutar `python -m pytest tests/contract -q` y `ruff check tests/contract scripts`.

El workflow `contract-drift.yml` corre manualmente o los lunes: descarga cada
`url` del lock y compara hash, versión y `x-estado`. No corre en PRs ni participa
del gate `test`; su fallo solo avisa que hay que revisar el upstream.

## Excepciones documentadas

### G7 no está publicado

El OpenAPI de G7 en `fa93a88` declara `info.version: 1.2.0` y
`info.x-estado: revision-local-no-publicada`. No hay servicio publicado ni mock
Prism estable para esta revisión. Los tests de `shared/test_contract_g7_*.py`
usan el contrato del repositorio y `respx`; el contrato se trata como fuente de
verdad aunque no esté desplegado. El lock registra ese estado y el drift checker
avisa si cambia.

### G2 no tiene OpenAPI machine-readable

`G2-Despacho-Specs` publica el contrato como Markdown
(`integraciones/api-contract.md`). No existe `openapi.yaml` que se pueda validar
ni generar fixtures automáticamente. El fixture `g2_api_contract.md` es la copia
completa del documento y los JSON de `mod_shipping/fixtures/` son los ejemplos
de las secciones 5.1 (cotización) y 6.1 (seguimiento) transcritos a mano. Esta
limitación se repite en `tests/contract/mod_shipping/README.md`.

### Casos de #213

`#213` ya está en `development`. Los fixtures de G7 incluyen
`esPredeterminada` y los tests de pedidos cubren la forma actual del contrato;
`csatEnviado` no aparece en `ventas.yaml`, así que no se fija en `shared/`.

### CSAT 403/404 (issue #84)

La issue #84 se cerró con la decisión `404 RECURSO_NO_ENCONTRADO`. Los casos
`403 ACCESO_DENEGADO` y `404 PEDIDO_NO_ENCONTRADO` se agregan en el PR de
`mod_postventa` (#229), no en `shared/`; ver el README del módulo.
