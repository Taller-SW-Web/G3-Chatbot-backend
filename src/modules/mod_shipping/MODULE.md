# `mod-shipping` — Envío

- **Owner:** @Taller-SW-Web/chatbot
- **Backend:** paquete `mod_shipping`, `basePath` `/direcciones` y `/envio`
- **Frontend:** carpeta `mod-shipping`, bloque de envío en `[/checkout]`
- **Depende de:** `mod-cart` (solo por `public.py`). Consigue las líneas del
  carrito con `provide_cart_repositories`, declarado en el manifiesto.
- **Contrato:** `docs/openapi.json`, tag `mod-shipping`.

## Responsabilidad

Cotización de envío contra G2 Despacho y direcciones guardadas del cliente en
G1 Seguridad. **No** guarda direcciones propias ni modifica el carrito.

## Superficie pública

| Tipo | Archivo | Qué expone |
|---|---|---|
| Backend | `public.py` | `DispatchPort`, `provide_despacho_client`, `provide_shipping_service` y los helpers de cotización (`build_destination`, `build_quote_lines`, `parse_quote`) que reutiliza `mod-checkout` |
| Frontend | `public.ts` | Tipos de cotización y dirección |

## Rutas

| Método | Ruta | Caso de uso |
|---|---|---|
| GET | `/api/v1/direcciones` | `ManageShippingUseCase.saved_addresses` |
| POST | `/api/v1/envio/cotizar` | `ManageShippingUseCase.quote` |

## Dependencias externas

- **G2 Despacho** `POST /api/v1/cotizaciones` con service token (4 s de timeout).
- **G1 Seguridad** `GET /direcciones` con el token del titular.
- `mod-cart.public.provide_cart_repositories` (dependencia declarada).

## Pruebas

- Unitarias: `tests/unit/mod_shipping/`
- Integración: `tests/integration/mod_shipping/`
- Contrato: endpoint ↔ `docs/openapi.json` (tag `mod-shipping`)

## Reglas

- No importar `domain/`, `application/` ni `adapters/` de otro módulo.
- No tocar `shared/` sin label `cross-modulo`.
- Un PR de este módulo no modifica archivos de otro `mod-*`.
