# `mod-auth` — Autenticación y cuenta

- **Owner:** @Ankarmin
- **Backend:** paquete `mod_auth`, `basePath` `/sesion`
- **Frontend:** carpeta `mod-auth`, rutas `[/cuenta]`, `[/verificar-correo]`
- **Depende de:** nadie. El contrato de Seguridad (puertos, cliente HTTP, JWKS y
  service token) vive en `src/shared/infrastructure/auth` para que cualquier
  módulo autentique al titular sin importar este paquete.
- **Contrato:** `docs/openapi.json`, tag `mod-auth`.

## Responsabilidad

Sesión del cliente contra Seguridad (SPEC-01..04): registro, login, MFA, refresh,
logout, perfil y verificación de correo. **No** incluye carrito, checkout ni
pedidos; `merge_cart_lines` se queda aquí porque el login es su único punto de
entrada.

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

## Dependencias externas

- **G1 Seguridad** vía `shared/application/ports/security_port.py` y
  `shared/infrastructure/http/security_client.py` (login, MFA, registro, perfil).
- `local_phone_verification` (tabla compartida) para `celularVerificado`.
- Capacidad `checkout_document` (registrada por `mod-checkout`) para
  `ultimoDocumento` sin importar el interior de checkout.

## Pruebas

- Unitarias: `tests/unit/mod_auth/`
- Integración: `tests/integration/mod_auth/` (no requiere tablas propias)
- Contrato: endpoints ↔ `docs/openapi.json` (tag `mod-auth`)

## Reglas

- No importar `domain/`, `application/` ni `adapters/` de otro módulo.
- No tocar `shared/` sin label `cross-modulo`.
- Un PR de este módulo no modifica archivos de otro `mod-*`.
