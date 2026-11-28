# `mod-auth` — Autenticación y cuenta

- **Owner:** @Taller-SW-Web/chatbot
- **Backend:** paquete `mod_auth`, `basePath` `/sesion`
- **Frontend:** carpeta `mod-auth`, rutas `[/cuenta]`, `[/verificar-correo]`
- **Depende de:** nadie. El contrato de Seguridad (puertos, cliente HTTP, JWKS y
  service token) vive en `src/shared/infrastructure/auth` para que cualquier
  módulo autentique al titular sin importar este paquete.
- **Contrato:** `docs/openapi.json`, tag `mod-auth`.

## Responsabilidad

Sesión del cliente contra Seguridad (SPEC-01..04): registro, login, MFA, refresh,
logout, perfil y verificación de correo. **No** incluye carrito, checkout ni
pedidos: la fusión del carrito anónimo se delega a la capacidad `CART_MERGE`
(SPEC-03 Req. 4) sin importar el interior de `mod-cart`.

## Superficie pública

| Tipo | Archivo | Qué expone |
|---|---|---|
| Backend | `public.py` | `provide_manage_session` (wiring) y los helpers de masking que otros módulos documentan |
| Frontend | `public.ts` | Tipos de sesión y guardas de `/cuenta` |

Ningún módulo importa `domain/`, `application/` ni `adapters/` de este paquete.

## Rutas

| Método | Ruta | Caso de uso |
|---|---|---|
| POST | `/api/v1/sesion/login` | `ManageSessionUseCase.login` |
| POST | `/api/v1/sesion/mfa/solicitar` | `ManageSessionUseCase.request_mfa` |
| POST | `/api/v1/sesion/mfa/verificar` | `ManageSessionUseCase.verify_mfa` |
| POST | `/api/v1/sesion/refresh` | `ManageSessionUseCase.refresh` |
| POST | `/api/v1/sesion/logout` | `ManageSessionUseCase.logout` |
| GET | `/api/v1/sesion/perfil` | `ManageSessionUseCase.profile` |
| POST | `/api/v1/sesion/registro` | `ManageSessionUseCase.register` |
| GET | `/api/v1/sesion/politica-contrasena` | `ManageSessionUseCase.password_policy` |
| POST | `/api/v1/sesion/verificar-correo` | `ManageSessionUseCase.verify_email` |
| POST | `/api/v1/sesion/verificar-correo/reenviar` | `ManageSessionUseCase.resend_verification` |
| POST | `/api/v1/contacto/celular/solicitar-otp` | `ManagePhoneVerificationUseCase.request_otp` |
| POST | `/api/v1/contacto/celular/verificar-otp` | `ManagePhoneVerificationUseCase.verify_otp` |

## Verificación local del celular (SPEC-04)

- OTP propio de 6 dígitos, TTL 5 min, 3 intentos y 3 envíos por ventana de
  15 min (`PHONE_OTP_*`); el código se guarda solo como HMAC-SHA256.
- `SimulatedSmsSender` registra el código en dev; con `MODO_QA=true` también
  se devuelve en la respuesta para pruebas. Nunca viaja al LLM ni a `message`.
- La verificación vale solo para el celular exacto de `GET /auth/me`; si el
  cliente lo cambia en Seguridad, se exige verificar de nuevo.
- `REQUIRE_PHONE_VERIFICATION` (opt-in, default `false` también en producción)
  activa el bloqueo legado `403 CELULAR_NO_VERIFICADO` en checkout; el guard de
  producción es `REQUIRE_VERIFIED_CONTACT`.

## Fusión del carrito anónimo (SPEC-03 Req. 4)

- `POST /sesion/login` y `POST /sesion/mfa/verificar` reciben el `chat_sid` de
  la cookie, lo hashean (igual que `caller_identity`) y pasan ese sid
  almacenado al caso de uso. Nunca viaja el valor crudo.
- La capacidad opcional `CART_MERGE` (registrada por `mod-cart` en
  `configure()`) se resuelve en tiempo de request en `provide_manage_session`.
  Si falta, un null object devuelve `merged=False` y el login sigue.
- Invariante de orden: `merge_for_login` se ejecuta **antes** de
  `link_anonymous_to_customer`, que limpia `Conversation.anonymous_sid` y es
  la clave con la que se buscan los carritos anónimos. Ambas escrituras
  comparten el `UnitOfWork` del request y se confirman juntas; si la fusión
  falla, se hace rollback y el login no abre sesión, dejando el carrito
  anónimo intacto para el siguiente intento.
- La respuesta de sesión expone `carritoFusionado`, `avisoCarrito` (el
  `CART_MERGED_MESSAGE` que muestra el chat) y `ajustesCarrito`
  (`sku`, `solicitado`, `ajustado`, `motivo`) para los ajustes por límite o
  stock.

## Dependencias externas

- **G1 Seguridad** vía `shared/application/ports/security_port.py` y
  `shared/infrastructure/http/security_client.py` (login, MFA, registro, perfil).
- `local_phone_verification` (tabla compartida) para `celularVerificado`.
- `phone_otp_challenge` (tabla compartida) para el OTP local de SPEC-04.
- Capacidades opcionales, resueltas por el registry (ADR-0024):
  - `checkout_document` (registrada por `mod-checkout`) para `ultimoDocumento`
    sin importar el interior de checkout.
  - `CART_MERGE` (registrada por `mod-cart`) para fusionar el carrito anónimo
    en el login sin importar el interior de carrito.
  Si falta una, el perfil/login usan un null object y `/health` reporta
  `degraded`.

## Pruebas

- Unitarias: `tests/unit/mod_auth/`
- Integración: `tests/integration/mod_auth/` (no requiere tablas propias)
- Contrato: endpoints ↔ `docs/openapi.json` (tag `mod-auth`)

## Reglas

- No importar `domain/`, `application/` ni `adapters/` de otro módulo.
- No tocar `shared/` sin label `cross-modulo`.
- Un PR de este módulo no modifica archivos de otro `mod-*`.
