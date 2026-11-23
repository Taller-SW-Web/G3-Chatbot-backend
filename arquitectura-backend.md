# Arquitectura Backend — Canal Chatbot

> **BFF FastAPI en monolito modular (ADR-0024).** Corte por capacidad de negocio:
> `src/shared/` (kernel) y `src/modules/mod_*/` (8 módulos). Dentro de cada módulo
> se conserva el corte hexagonal de ADR-0002 (`domain/`, `application/`,
> `ports/`, `adapters/`). La fuente de verdad de las fronteras es
> [`modules.manifest.json`](modules.manifest.json) y el gate
> [`scripts/check_modules.py`](scripts/check_modules.py); las decisiones están en
> [`docs/adr/ADR-0024-modularizacion.md`](docs/adr/ADR-0024-modularizacion.md).
>
> Si este documento contradice una spec de `G3-Chatbot-specs/openspec/specs/`,
> manda la spec. Si contradice el manifiesto o el checker, mandan ellos.

## 1. Reglas que no se negocian

1. **Dependencias hacia adentro** dentro de cada módulo:
   `adapters → application → domain`; `ports` son los contratos. La lógica de
   negocio no conoce FastAPI, httpx, SQLAlchemy ni variables de entorno.
2. **Fronteras entre módulos**: un módulo importa solo `src.shared.*`, su propio
   árbol y `src.modules.<otro>.public`. Nunca `domain/`, `application/` ni
   `adapters/` ajenos.
3. **Sin listas manuales**: `src/infrastructure/main.py` descubre
   `src/modules/*/module.py`; añadir un módulo no edita el composition root.
4. **Contrato estable**: cada router declara `tags=[MODULE_ID]` y las rutas
   públicas no cambian al mover código. `docs/openapi.json` se exporta con
   `python scripts/export_openapi.py`.
5. **Un PR toca un módulo**. `shared/` exige label `cross-modulo` y revisión del
   owner de plataforma.
6. **Toda capacidad cruzada es explícita**: o se declara en `dependsOn` y se
   importa `public.py`, o el módulo dueño la registra en el registry y el
   consumidor la resuelve. No hay `None` silencioso: al arrancar se validan las
   capacidades requeridas y `/health` reporta las opcionales ausentes.

## 2. Árbol real

```text
src/
  shared/                         # kernel: sin reglas de negocio de un módulo
    application/
      dto/                        # contratos de comercio, order_ref, outbox
      ports/                      # unit_of_work, security, token, phone,
                                  # checkout_document, checkout_snapshot,
                                  # conversation_context, notification,
                                  # conversation/message repositories
      tools/                      # contratos de tools y ExecutionContext
    domain/                       # caller_identity, block, masking, errores,
                                  # request_context, sensitive_data_filter,
                                  # entidades conversation/message
    infrastructure/
      auth/                       # JWKS, service token, SecurityClient
      config/                     # Settings (12-Factor, zod-like al arranque)
      db/                         # engine/session, 14 modelos, migración 0001
      di/                         # registry de capacidades, container
      fakes/                      # Seguridad y Ventas en memoria
      http/                       # problem+json, correlation, auth guard,
                                  # rate limit, health, router options
      persistence/                # outbox, order_ref, phone verification,
                                  # conversation/message/attachment, UoW
      worker/                     # outbox loop
  modules/
    mod_auth/                     # sesión y cuenta                 (/sesion)
    mod_catalog/                  # catálogo                        (/catalogo)
    mod_cart/                     # carrito y cupón                 (/carrito)
    mod_shipping/                 # envío y direcciones             (/envio, /direcciones)
    mod_checkout/                 # checkout, pago y outbox         (/checkout)
    mod_orders/                   # pedidos, seguimiento y correo   (/pedidos)
    mod_postventa/                # devoluciones, reclamos, CSAT    (/devoluciones, /reclamos)
    mod_chat/                     # conversaciones, SSE, tools, LLM (/chat/conversaciones)
  infrastructure/
    main.py                       # composition root: descubre y monta
    discovery.py                  # pkgutil + configure_modules + capacidades
```

Cada módulo expone:

| Archivo | Contrato |
|---|---|
| `MODULE.md` | owner, `basePath`, rutas, dependencias, capacidades y pruebas. |
| `module.py` | `MODULE_ID`, `configure(container)`, `build_router(options)` / `router`, providers y `__all__`. |
| `public.py` | única superficie importable por otros módulos. |
| `__init__.py` | literal canónico `MODULE_ID = "mod-x"`. |
| `domain/` | reglas puras y value objects. |
| `application/` | casos de uso; orquestan puertos, no adapters ni ORM. |
| `ports/` | contratos propios del módulo. |
| `adapters/` | HTTP, clientes, persistencia, fakes; único lugar con I/O. |

## 3. Los 8 módulos

| id | Owner | `basePath` | Depende de | Responsabilidad |
|---|---|---|---|---|
| `mod-auth` | @Taller-SW-Web/chatbot | `/sesion` | nadie | registro, login, MFA, refresh, logout, perfil, verificación de correo. |
| `mod-catalog` | @Taller-SW-Web/chatbot | `/catalogo` | nadie | búsqueda, detalle, disponibilidad, promociones y reglas puras de producto. |
| `mod-cart` | @Taller-SW-Web/chatbot | `/carrito` | `mod-catalog` | carrito anónimo/cliente, ítems, cupón, stock y totales. |
| `mod-shipping` | @Taller-SW-Web/chatbot | `/envio`, `/direcciones` | `mod-cart` | cotización G2 y direcciones guardadas G1. |
| `mod-checkout` | @Taller-SW-Web/chatbot | `/checkout` | `mod-cart`, `mod-shipping` | checkout, pago simulado, snapshot y outbox transaccional. |
| `mod-orders` | @Taller-SW-Web/chatbot | `/pedidos` | `mod-checkout` | listado/detalle, seguimiento G2 y confirmación por correo. |
| `mod-postventa` | @Taller-SW-Web/chatbot | `/devoluciones`, `/reclamos`, `/evidencias` | `mod-orders` | devoluciones, reclamos, evidencias y CSAT contra G5. |
| `mod-chat` | @Taller-SW-Web/chatbot | `/chat/conversaciones` | todos | conversaciones, SSE, bucle LLM-tool y acciones directas. |

`mod-auth` es la base: el contrato de Seguridad (puertos, cliente HTTP, JWKS y
service token) vive en `src/shared/infrastructure/auth` para que cualquier
módulo autentique al titular sin importar `mod_auth`.

## 4. DAG

```text
catalog ← cart ← checkout ← orders ← postventa
             ↑        ↑
          shipping ───┘
chat → todos (orquestador)
auth → usado por todos vía shared/auth
```

- `chat` puede depender de todos; **ningún módulo importa chat**.
- `shipping` declara `mod-cart` y consume `provide_cart_repositories` desde su
  `public.py`; no hay capacidad oculta.
- Un cambio de DAG se hace en `modules.manifest.json` **y** en este documento en
  el mismo PR.

## 5. `shared/`: kernel, no cajón de sastre

`shared/` contiene solo lo que es transversal y no pertenece a un módulo:

- **Contratos**: `UnitOfWorkPort`, `SecurityPort`, `AccessTokenValidatorPort`,
  `PhoneVerificationPort`, `CheckoutDocumentPort`, `CheckoutSnapshotPort`,
  `ConversationContextPort`, `NotificationRepositoryPort`, `EmailOutboxPort`,
  `ConfirmationSourcePort`.
- **HTTP**: problem+json, correlation id, auth de transporte, rate limit,
  `/health`, `RouterOptions`.
- **Datos**: `Base`, engine/session, 14 modelos y la migración única de Alembic.
- **Persistencia compartida**: outbox, `order_ref`, `local_phone_verification`,
  conversación/mensaje/adjunto y `SqlAlchemyUnitOfWork`.
- **Fakes de módulos externos**: Seguridad y Ventas en memoria (`MODULES_MODE`).

`shared` **no importa** `src.modules.*`. El checker lo verifica.

### Puerto de contexto de conversación (H-02)

`mod-chat` es dueño de conversaciones y mensajes. `mod-cart` y `mod-catalog`
solo necesitan memoria de trabajo (`ultimoCarrusel`, `filtrosVigentes`),
ownership, `touch` y la fila TOOL de auditoría. Para no invertir el DAG ni
importar la persistencia de chat:

1. `ConversationContextPort` vive en
   `src/shared/application/ports/conversation_context_port.py`.
2. `mod-chat` lo implementa en
   `src/modules/mod_chat/adapters/conversation_context_adapter.py`.
3. `mod-chat.configure()` registra la capacidad `conversation_context`.
4. Cart/catalog la resuelven en tiempo de request; el checker no ve imports
   cruzados.

### Snapshot de checkout (H-03)

`order_ref`, `checkout` y `payment_attempt` son tablas de `mod-checkout`.
`mod-orders` construye el correo con `CheckoutSnapshotPort` (shared) y
`provide_confirmation_outbox`, ambos servidos por `mod-checkout.public`. Orders
no consulta esas tablas ni importa sus modelos.

## 6. Auto-discovery y composition root

`src/infrastructure/discovery.py`:

1. recorre `src/modules/*/module.py` con `pkgutil`;
2. exige `MODULE_ID` y `configure(container)` (un módulo sin `configure` falla,
   no se salta en silencio);
3. ejecuta `configure(container)` de todos;
4. valida `REQUIRED_CAPABILITIES`: si falta una, aborta el arranque con el
   módulo que la requiere en el mensaje;
5. registra `OPTIONAL_CAPABILITIES` ausentes, las loguea como `ERROR` y las
   expone en `/health` como `{"status": "degraded", "missingCapabilities": [...]}`.

`main.py` no conoce módulos concretos: descubre, monta
`build_router(RouterOptions(...))` bajo `/api/v1` y añade middleware/health. El
`ACTION_SCOPE` se resuelve de forma perezosa desde el registry, no con un import
a `mod_chat`.

### Capacidades actuales

| Capacidad | La registra | La consume | ¿Requerida? |
|---|---|---|---|
| `conversation_context` | `mod-chat` | `mod-cart`, `mod-catalog` | sí |
| `order_confirmation` | `mod-orders` | `mod-checkout`, worker | sí para checkout |
| `dispatch_port` | `mod-shipping` | `mod-orders` | sí |
| `checkout_document` | `mod-checkout` | `mod-auth` | no (perfil degrada) |
| `outbox_processor` | `mod-checkout` | worker compartido | sí en producción |
| `action_scope` | `mod-chat` | composition root | sí en producción |

## 7. Contrato HTTP y OpenAPI

- Prefijo global: `/api/v1`.
- Cada router usa `APIRouter(..., tags=[MODULE_ID])`; el checker falla si falta.
- `docs/openapi.json` se regenera con `python scripts/export_openapi.py` y se
  verifica en CI con `python scripts/export_openapi.py --check`.
- Los cambios de ruta o método son aditivos salvo decisión de spec. Un endpoint
  nuevo actualiza `MODULE.md`, el test del router y el OpenAPI en el mismo PR.

### Mapa de rutas

| Módulo | Rutas |
|---|---|
| `mod-auth` | `POST /sesion/login`, `/registro`, `/mfa/solicitar`, `/mfa/verificar`, `/refresh`, `/logout`, `/verificar-correo`, `/verificar-correo/reenviar`; `GET /sesion/perfil`, `/politica-contrasena` |
| `mod-catalog` | `GET /catalogo/productos`, `/productos/{id}`, `/promociones`, `/disponibilidad` |
| `mod-cart` | `GET/DELETE /carrito`; `POST/PATCH/DELETE /carrito/items`; `POST/DELETE /carrito/cupon` |
| `mod-shipping` | `GET /direcciones`; `POST /envio/cotizar` |
| `mod-checkout` | `POST /checkout`; `GET /checkout/{id}`; `POST /checkout/{id}/pago` |
| `mod-orders` | `GET /pedidos`, `/pedidos/{id}`, `/pedidos/{id}/seguimiento`; `POST /pedidos/{id}/reenviar-confirmacion` |
| `mod-postventa` | `POST/GET /devoluciones`, `GET /devoluciones/{id}`; `POST/GET /reclamos`, `GET /reclamos/{codigo}`; `POST /evidencias`; `POST /pedidos/{id}/csat` |
| `mod-chat` | `POST/GET /chat/conversaciones`, `GET /chat/conversaciones/buscar`, `GET/POST /chat/conversaciones/{id}/mensajes` (SSE/acción directa) |

## 8. Gate de fronteras

`python scripts/check_modules.py` verifica con `ast` (sin dependencias):

- estructura: paquete, `MODULE.md`, `public.py`, `MODULE_ID` literal;
- `module.py`: `configure` + `router`/`build_router`;
- imports: resuelve `ImportFrom.level` (relativos), valida destinos constantes de
  `importlib.import_module`/`__import__` y rechaza los dinámicos;
- `shared` no importa módulos;
- composition root solo importa `src.modules.<id>.module`;
- routers con `tags=[MODULE_ID]`.

La única excepción dinámica permitida es `src/infrastructure/discovery.py`
(importa el nombre que descubre `pkgutil`). Cualquier otra se reporta como
violación. `tests/unit/test_check_modules.py` es la regresión del gate.

## 9. Datos y migraciones

- Una sola cadena de Alembic en `src/shared/infrastructure/db/migrations`;
  nombre `YYYYMMDD_mod_<id>_<desc>`.
- 14 modelos en `src/shared/infrastructure/db/models`: `conversation`, `message`,
  `attachment`, `local_phone_verification`, `cart`, `cart_item`, `checkout`,
  `payment_attempt`, `order_ref`, `notification`, `claim_ref`, `return_ref`,
  `evidence`, `outbox`.
- **Aplicación no importa modelos ORM**: construye DTOs de inserción
  (`NewCheckout`, `NewPaymentAttempt`, `NewOutboxTask`, `NewOrderReference`,
  referencias de postventa) y el adapter PostgreSQL las convierte en filas.
- **Transacciones**: checkout + intento + outbox comparten `SqlAlchemyUnitOfWork`;
  los adapters hacen `flush`, nunca `commit`.

## 10. Flujos de referencia

### Turno de texto

1. `POST /chat/conversaciones/{id}/mensajes` valida sesión, ownership y rate
   limit; responde SSE.
2. `InterpretAndRespondUseCase` carga historial, redacta datos sensibles y llama
   al `LLMProvider` (timeout configurable).
3. El LLM pide una tool; `RegistryToolAdapter` valida args y ejecuta el caso del
   módulo dueño (catálogo, carrito, pedidos).
4. La respuesta se persiste y se emite como `token*` + bloques + `fin`; si el
   LLM falla, degrada sin romper el turno.

### Checkout y pago

1. `POST /checkout` exige `Idempotency-Key`, carrito activo, contacto, dirección
   y cotización server-side de Despacho.
2. Crea `checkout` + `order_ref` + tarea outbox en la misma transacción.
3. `POST /checkout/{id}/pago` introspecciona el token, simula la tarjeta y
   persiste solo marca + últimos 4.
4. Aprobado: encola `NOTIFY_SALES_PAYMENT` y pide la confirmación por correo a
   `mod-orders`; expirado/rechazado: `REQUEST_CANCELLATION`.
5. El worker del outbox reintenta con backoff y DLQ lógica (`FAILED`).

## 11. Añadir un endpoint o una capacidad

1. Lee la spec del módulo y `MODULE.md`; si el cambio no está en la spec,
   actualízala en el mismo PR.
2. Define/extiende el puerto en `ports/`; no importes ORM ni adapters en
   `application/`.
3. Implementa la regla en `domain/` si es pura; orquesta en `application/`.
4. Añade el adapter en `adapters/` y el DTO en `adapters/http/`.
5. Cablea en `module.py` (`provide_*`, `build_router`, `configure`).
6. Añade test unitario (regla) e integración de router (respx/TestClient).
7. Actualiza `public.py` solo si otro módulo debe consumirlo. Si es capacidad,
   declara `REQUIRED_CAPABILITIES`/`OPTIONAL_CAPABILITIES` y actualiza el
   registry, el manifiesto y este documento.
8. Ejecuta `pytest tests/unit -q`, `ruff check .`, `mypy src`,
   `python scripts/check_modules.py` y
   `python scripts/export_openapi.py --check`.

## 12. Verificación y deuda conocida

Comandos obligatorios en cada PR:

```bash
pytest tests/unit -q
ruff check .
mypy src
python scripts/check_modules.py
python scripts/export_openapi.py --check
```

Deuda conocida y hallazgos abiertos se registran en `docs/flujo-de-trabajo.md` y
en el issue correspondiente. El más relevante hoy (H-17): el frontend llama
`POST /api/v1/contacto/celular/verificar-otp` (y la spec define también
`solicitar-otp`), pero el backend no expone esos endpoints ni tiene todavía el
almacén/adaptador de OTP; no se implementó para no inventar una decisión de
producto y de persistencia. Ver la evidencia en el reporte del PR.

## 13. Enlaces

- [ADR-0024 — monolito modular](docs/adr/ADR-0024-modularizacion.md)
- [Manifiesto de módulos](modules.manifest.json)
- [Checker de fronteras](scripts/check_modules.py)
- [Flujo de trabajo](docs/flujo-de-trabajo.md)
- [Reglas para agentes](AGENTS.md)
- `MODULE.md` de cada módulo (owner, rutas, dependencias y capacidades).
