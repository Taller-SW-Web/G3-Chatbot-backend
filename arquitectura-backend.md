# Arquitectura Backend — Canal Chatbot

> **BFF hexagonal FastAPI.** Estado: **fase 0 — 105 ficheros en `src/`, todos en `0 bytes`**, `tests/` solo con `.gitkeep`. Estructura alineada a `../../PROYECTO CHATBOT/ARQUITECTURA-ICEPANEL.md §5-§6`, `G3-Chatbot-specs/docs/arquitectura/c4.md` (L2 + 3a + 3b) y `ADR-0002, 0005-0018`.
> Regla: si contradice a una spec de `G3-Chatbot-specs/openspec/specs/`, manda la spec.

## 0. Cómo leer este repo

Este backend es un **BFF hexagonal** (puertos y adaptadores, ADR-0002). La idea en una frase: **la lógica de negocio no sabe nada de FastAPI, httpx ni PostgreSQL**; solo habla con contratos (`ports/`). Los detalles externos viven en `adapters/`.

Orden de lectura recomendado:

1. `src/ports/inbound/chatbot_service_port.py` — el contrato único que exponen REST y WS hacia adentro.
2. `src/application/use_cases/` — los 9 `Gestionar*` + `interpretar_responder`. Aquí vive el "qué hace el sistema".
3. `src/domain/` — entidades y reglas puras, sin imports de framework.
4. `src/adapters/inbound/http/` + `websocket/` — cómo entra el mundo exterior.
5. `src/adapters/outbound/` — cómo salimos (4 módulos + LLM + pago/SMS/correo + DB).
6. `src/infrastructure/` — quién cablea todo (DI, settings, DB, worker, prompts).

Regla de dependencias (memorizar): `inbound → ports → application → domain ← outbound`. Nada apunta hacia afuera desde `application/` o `domain/`. `infrastructure/` puede conocer a todos para cablearlos.

## 1. Árbol

```
G3-Chatbot-backend/
├── alembic.ini (0B, placeholder)
├── requirements.txt (0B)
├── arquitectura-backend.md (este archivo)
├── docs/ (html/png/json visual-check, sin código)
├── src/
│   ├── adapters/
│   │   ├── __init__.py
│   │   ├── inbound/http/ (12 routers + dependencies/3)
│   │   ├── inbound/websocket/ (1)
│   │   └── outbound/ (auth/1, http_clients/8, nlp/3, notificaciones/2, pagos/1, persistence/7+models/13)
│   ├── application/ (use_cases/10, tools/2, dto/2)
│   ├── domain/ (entities/8, services/9, value_objects/2)
│   ├── infrastructure/ (config/3, db/connection+migrations, worker/3, prompts/1, evals, main.py)
│   └── ports/ (inbound/1, outbound/10)
└── tests/ (unit, integration, evals — solo .gitkeep)
```

## 2. Raíz e infraestructura transversal

| Fichero | Propósito | Origen | Estado |
|---|---|---|---|
| `alembic.ini` | Config migraciones: `sqlalchemy.url`, `script_location=src/infrastructure/db/migrations` | `README §1.5` SQLAlchemy+Alembic, `modelo-datos.md` 13 tablas | Vacío, rellenar con env en fase 1 |
| `requirements.txt` | Deps `fastapi, pydantic v2, sqlalchemy, alembic, httpx, pyjwt, apscheduler, pytest, respx` | `README §1.5` | Vacío |
| `src/infrastructure/main.py` | App FastAPI + lifespan (routers + worker) | `c4.md L2` BFF | Vacío |
| `src/infrastructure/config/settings.py` | URLs base `SEG/PRO/VEN/DES`, LLM, SMTP, JWKS, timeouts | `ADR-0017`, `contratos §3` | Vacío |
| `src/infrastructure/config/container.py` | DI: cablea casos, clientes, repos, providers | `ADR-0002` | Vacío |
| `src/infrastructure/config/pesos_por_categoria.yaml` | Parche peso carrito hasta cerrar A6 | SPEC-12 Req.4, A6 | Vacío |
| `src/infrastructure/db/connection.py` | Engine/sesión SQLAlchemy async | `modelo-datos.md` | Vacío |
| `src/infrastructure/db/migrations/` + `versions/` | Migraciones 13 tablas | `modelo-datos.md` | Solo `.gitkeep` |
| `src/infrastructure/worker/outbox_worker.py, jobs.py, scheduler.py` | `OutboxWorker NOTIFICAR_PAGO/SOLICITAR_ANULACION/ENVIAR_CORREO` + `ExpirarCheckouts 1min, limpieza carritos 7d, evidencias 24h` | SPEC-14,15,16,11,21, `ADR-0011` | Vacíos |
| `src/infrastructure/prompts/sistema.md` | Prompt sistema versionado | SPEC-05 RNF | Vacío |
| `src/infrastructure/evals/` | `pytest -m evals >=90%` 120 frases | SPEC-05 RNF | Solo `.gitkeep` |

Por qué existe `infrastructure/`: es el único lugar que conoce secretos, URLs y el framework. Si mañana Productos cambia su ruta (riesgo R1, A5), solo se toca un adapter; si cambia el proveedor LLM, solo `nlp/`; los casos no se enteran.

## 3. Adaptadores inbound (borde HTTP/WS)

| Fichero | Propósito | Origen | Estado |
|---|---|---|---|
| `adapters/inbound/http/chatbot_router.py` | Conversaciones/mensajes/acciones `POST/GET /chat/conversaciones, /buscar, /{id}/mensajes → 202` | SPEC-05, `contratos §2.1` | Vacío |
| `adapters/inbound/http/sesion_router.py` | `POST /sesion/registro, /login, /mfa/*, /refresh, /logout, GET /sesion/perfil, /politica-contrasena` | SPEC-01..03, `contratos §2.2` | Vacío |
| `adapters/inbound/http/contacto_router.py` | `POST /contacto/celular/solicitar-otp, /verificar-otp` propio | SPEC-04, A2 | Vacío |
| `adapters/inbound/http/catalogo_router.py` | `GET /catalogo/productos, /{id}, /promociones, /disponibilidad` | SPEC-06..10, `contratos §2.3` | Vacío |
| `adapters/inbound/http/carrito_router.py` | `GET/DELETE /carrito, POST/PATCH/DELETE items, POST/DELETE /carrito/cupon` | SPEC-11,13, `contratos §2.4` | Vacío |
| `adapters/inbound/http/envio_router.py` | `POST /envio/cotizar` + `GET /direcciones` proxy | SPEC-12, `contratos §2.4` | Vacío |
| `adapters/inbound/http/checkout_router.py` | `POST /checkout, GET /checkout/{id}, POST /{id}/pago` + `Idempotency-Key` | SPEC-14, `contratos §2.5` | Vacío |
| `adapters/inbound/http/pedidos_router.py` | `GET /pedidos, /{id}, /{id}/seguimiento, POST /{id}/reenviar-confirmacion` | SPEC-15..18 | Vacío |
| `adapters/inbound/http/reclamos_router.py` | `POST/GET /reclamos, GET /reclamos/{codigo}` | SPEC-19,20 | Vacío |
| `adapters/inbound/http/evidencias_router.py` | `POST /evidencias` proxy upload Ventas | SPEC-21, A13 | Vacío |
| `adapters/inbound/http/devoluciones_router.py` | `POST/GET /devoluciones, GET /{id}` | SPEC-21,22 | Vacío |
| `adapters/inbound/http/admin_router.py` | `GET /admin/outbox-fallidos` interno | `grabacion-pedido/design.md` | Vacío |
| `adapters/inbound/http/dependencies/rate_limiter.py` | 20 msg/min, 5 cupones/10min, 5 registros/10min | SPEC-05 Req.11, SPEC-13, SPEC-01 | Vacío |
| `adapters/inbound/http/dependencies/jwt_validator.py` | JWKS cache, `iss=auth-service tipo=acceso rol CLIENTE` | `contratos §1`, SPEC-03 | Vacío |
| `adapters/inbound/http/dependencies/checkout_guard.py` | `exigir_celular_verificado()` 403 `CELULAR_NO_VERIFICADO` | SPEC-04, SPEC-14 Req.1 | Vacío |
| `adapters/inbound/websocket/chatbot_ws_adapter.py` | WS `wss://.../api/v1/chat/ws?conversacionId=` eventos `token/bloque/fin/error` | SPEC-05 Req.4 | Vacío |

Qué NO hacen los routers: sin reglas de negocio, sin httpx, sin SQL. Solo validan (Pydantic), aplican `dependencies/`, llaman al caso vía `ChatbotServicePort` y mapean errores a `problem+json` con `code`.

## 4. Núcleo application

| Fichero | Propósito | Origen | Estado |
|---|---|---|---|
| `application/use_cases/gestionar_conversacion_use_case.py` | Crear/listar/buscar/historial `cliente_id/chat_sid` | SPEC-05, `ADR-0018` | Vacío (creado fase 0) |
| `application/use_cases/interpretar_responder_use_case.py` | Ciclo LLM-tool-LLM max 5 it, prompt+resumen+12 msgs, publica WS | SPEC-05 Req.3,4,7 | Vacío (renombrado) |
| `application/use_cases/gestionar_sesion_use_case.py` | SPEC-01..04: `SesionService.post_login`, fusiona carrito, `accionPendiente` | SPEC-01..04, hueco `ADR-0018` | Vacío (renombrado) |
| `application/use_cases/gestionar_catalogo_use_case.py` | SPEC-06..09: `CatalogoService, Normalizador, Recomendacion, CrossSell, Promociones, PricingAdapter, VarianteResolver, StockValidator, Similares` | SPEC-06..09 | Vacío (renombrado) |
| `application/use_cases/gestionar_carrito_use_case.py` | SPEC-10,11,13: `CarritoService, TotalesCalculator, ResolverLinea, CuponService`, límites 10/20 | SPEC-10,11,13 | Vacío (renombrado) |
| `application/use_cases/gestionar_checkout_use_case.py` | SPEC-12,14: preconds ordenadas, 15 min, 3 intentos, `DocumentoValidator` A14 | SPEC-12,14 | Vacío (renombrado) |
| `application/use_cases/gestionar_pedido_use_case.py` | SPEC-15,16,17: `PedidoService, SnapshotBuilder, NotificacionService, EstadoMapper` | SPEC-15,16,17 | Vacío (renombrado) |
| `application/use_cases/gestionar_seguimiento_use_case.py` | SPEC-18: `SeguimientoMapper` lista blanca | SPEC-18, A11 | Vacío (repurposed) |
| `application/use_cases/gestionar_postventa_use_case.py` | SPEC-19..22: `ReclamoService, DevolucionService` + consultas | SPEC-19..22 | Vacío (repurposed) |
| `application/use_cases/gestionar_evidencias_use_case.py` | Upload evidencia `POST /evidencias` previo a devolución | SPEC-21, A13 | Vacío (creado) |
| `application/tools/tool_registry.py, action_dispatcher.py` | Catálogo tools Pydantic + botones sin LLM | SPEC-05 Req.3,6,8 | Vacíos |
| `application/dto/mensaje_entrante_dto.py, respuesta_chatbot_dto.py` | DTOs turno | SPEC-05 | Vacíos |

Los casos orquestan: piden datos a puertos, aplican reglas de `domain/services`, persisten vía repos y devuelven DTOs. Nunca importan FastAPI, httpx ni SQLAlchemy.

## 5. Adaptadores outbound + puertos

| Fichero | Propósito | Origen | Estado |
|---|---|---|---|
| `adapters/outbound/http_clients/seguridad_api_adapter.py` + `cliente_repository_port.py` | Registro/login/MFA/direcciones/introspección | `contratos §3.1`, SPEC-01..03,12,14 | Vacíos |
| `http_clients/introspeccion_api_adapter.py` | `POST /auth/introspeccion` 3 s sin caché, antes de pago | SPEC-14, A3 | Vacío |
| `http_clients/productos_api_adapter.py, inventario_api_adapter.py, cupones_api_adapter.py, evaluacion_api_adapter.py` + `producto_repository_port.py` | Catálogo/precios/disponibilidad/cupones/evaluar | `contratos §3.2` A5 (provisional) | Vacíos |
| `http_clients/ventas_api_adapter.py` + `pedido_repository_port.py` | Pedidos/reclamos/devoluciones, normaliza `codigo→code` 5 s | `contratos §3.3` v1.3.0 | Vacíos |
| `http_clients/despacho_api_adapter.py` + `despacho_port.py` | `POST /zonas/cotizar` + `GET /seguimiento?idPedido=` 4 s | `contratos §3.4` A11,A12 | Vacíos |
| `adapters/outbound/auth/service_token_provider.py` + `ports/outbound/service_token_port.py` | `client_credentials modulo-chatbot` cache hasta 60 s antes `exp`, Hito 4 | `ADR-0009`, SPEC-14,18 | Vacíos |
| `adapters/outbound/nlp/llm_provider_base.py, openai_nlp_adapter.py, claude_nlp_adapter.py` + `ports/outbound/llm_provider_port.py` | Tool calling streaming 15 s, config env | SPEC-05, `ADR-0005` | Vacíos |
| `adapters/outbound/pagos/payment_simulator_adapter.py` + `ports/outbound/payment_gateway_port.py` | Simulador determinista `SIM-...`, nunca persiste PAN/CVV | SPEC-14, `ADR-0014` | Vacíos |
| `adapters/outbound/notificaciones/smtp_email_adapter.py` + `ports/outbound/email_sender_port.py` | Jinja2 `CONFIRMACION+REENVIO max2`, reintentos SMTP max3 | SPEC-16 | Vacíos |
| `adapters/outbound/notificaciones/simulated_sms_adapter.py` + `ports/outbound/sms_sender_port.py` | OTP 6 díg 5 min 3 int | SPEC-04, `ADR-0015` | Vacíos |
| `adapters/outbound/persistence/conversacion_postgres_adapter.py, carrito_postgres_adapter.py, checkout_postgres_adapter.py, outbox_postgres_adapter.py, pedido/producto/cliente_postgres_adapter.py` + `ports/outbound/cliente_repository_port.py, conversacion_state_port.py` | Persistencia 13 tablas; `checkout+intento+outbox` misma transacción | `modelo-datos.md`, SPEC-15 Req.3 | Vacíos |
| `adapters/outbound/persistence/models/` 13: `conversacion, mensaje, celular_verificacion, carrito, item_carrito, checkout, intento_pago, pedido_ref, notificacion, reclamo_ref, devolucion_ref, evidencia, outbox` | Tablas propias, solo refs externas | `modelo-datos.md` | Vacíos |
| `ports/inbound/chatbot_service_port.py` | Contrato único REST+WS → casos | `motor-conversacion/design.md` | Vacío |

Eliminados fase 0 por duplicados: `pasarela_pago_port, nlp_engine_port, notificador_port, pasarela_pago_adapter, email_sms_adapter`.

## 6. Dominio

| Fichero | Propósito | Origen | Estado |
|---|---|---|---|
| `domain/entities/conversacion.py, mensaje.py, cliente.py, carrito.py, item_carrito.py, checkout.py, intento_pago.py, pedido.py` | Entidades puras | `modelo-datos.md`, `ADR-0002` | Vacías |
| `domain/services/sensitive_data_filter.py` | Redacta tarjeta Luhn/OTP/pass antes de LLM/persistir | SPEC-05 Req.9, `ADR-0006` | Vacío |
| `domain/services/output_validator.py` | Compara montos texto vs tools, corrige en `fin` | SPEC-05 Req.7 | Vacío |
| `domain/services/degraded_mode.py` | Keywords+menú si LLM >15 s/falla | SPEC-05 Req.10 | Vacío |
| `domain/services/totales_calculator.py, snapshot_builder.py, estado_mapper.py, seguimiento_mapper.py, politica_descuento.py, regla_validacion_carrito.py` | Reglas comercio | SPEC-11,15,17,18 | Vacías |
| `domain/value_objects/estado_conversacion.py, intencion.py` | `ACTIVA/ARCHIVADA/CERRADA`, intenciones | SPEC-05, `flujos (a)` | Vacías |

`domain/` no importa FastAPI, httpx, SQLAlchemy ni variables de entorno. Si un fichero necesita la hora, recibe el reloj por parámetro; si necesita un dato externo, lo recibe ya cargado desde el caso.

## 7. Flujo A — turno de texto (trazado archivo por archivo)

Caso: visitante escribe "busco zapatillas running talla 42" en `ChatPage`.

1. `POST /chat/conversaciones/{id}/mensajes` → `adapters/inbound/http/chatbot_router.py`. Aplica `dependencies/rate_limiter.py` (20/min) y `jwt_validator.py` (opcional sin token). Responde `202 {mensajeId}` sin esperar al LLM.
2. El router invoca `ports/inbound/chatbot_service_port.py` → `application/use_cases/interpretar_responder_use_case.py`.
3. El caso carga historial vía `adapters/outbound/persistence/conversacion_postgres_adapter.py` (últimos 12 + `resumen`) y pasa el texto por `domain/services/sensitive_data_filter.py` antes de persistirlo en `mensaje.py`.
4. Llama a `ports/outbound/llm_provider_port.py` (impl. `adapters/outbound/nlp/openai_nlp_adapter.py` o `claude_nlp_adapter.py`, timeout 15 s). El LLM devuelve tool call `buscar_productos`.
5. `application/tools/tool_registry.py` valida args Pydantic + sesión (`requiere_sesion`) y ejecuta el handler = `gestionar_catalogo_use_case.py`, que usa `http_clients/productos_api_adapter.py` (4 s) e `inventario_api_adapter.py` (3 s).
6. Con el resultado, el caso re-llama al LLM para redactar, publica `token*` y `bloque CARRUSEL_PRODUCTOS` por `adapters/inbound/websocket/chatbot_ws_adapter.py`, y `domain/services/output_validator.py` corrige montos en `fin`.
7. Si el LLM tarda >15 s o falla, `domain/services/degraded_mode.py` responde menú por REST. Si el WS está caído, el frontend hace polling (el backend no cambia).

## 8. Flujo B — checkout y pago (trazado)

Caso: cliente verificado pulsa "Confirmar y pagar S/ 299.90".

1. `POST /checkout` → `adapters/inbound/http/checkout_router.py` con `Idempotency-Key`. `dependencies/checkout_guard.py` exige celular verificado (403 si no).
2. `application/use_cases/gestionar_checkout_use_case.py` verifica en orden SPEC-14: sesión → celular (`models/celular_verificacion.py`) → carrito revalidado (`gestionar_carrito_use_case.py` + `inventario_api_adapter.py`) → dirección/cotización (`adapters/outbound/http_clients/despacho_api_adapter.py`) → cupón (`cupones_api_adapter.py`).
3. Crea `checkout` PENDIENTE 15 min + `pedido CREADO` en Ventas (`ventas_api_adapter.py`) + tarea outbox en **la misma transacción** (`checkout_postgres_adapter.py` + `outbox_postgres_adapter.py` + `models/checkout|intento_pago|outbox`).
4. `POST /checkout/{id}/pago` → introspección (`introspeccion_api_adapter.py` 3 s) → `payment_simulator_adapter.py` (`SIM-...`, guarda solo marca+ult4 en `models/intento_pago.py`).
5. APROBADO → `infrastructure/worker/outbox_worker.py` notifica a Ventas y encola correo (`smtp_email_adapter.py`, plantilla confirmación, `models/notificacion.py`). RECHAZADO x3 → FALLIDO + anulación `PAGO_NO_COMPLETADO`. `jobs.py/scheduler.py` expiran checkouts cada minuto.

## 9. Reglas por capa (qué sí / qué no)

| Capa | Sí | No |
|---|---|---|
| `adapters/inbound` | Validar DTOs, aplicar `dependencies/`, llamar 1 caso, mapear `code→HTTP` | Lógica de negocio, httpx, SQL |
| `application/use_cases` | Orquestar puertos, aplicar orden de precondiciones, controlar transacción | Importar FastAPI/httpx/SQLAlchemy, leer `os.environ` |
| `domain` | Reglas puras y testeables sin I/O | Cualquier import de framework o red |
| `adapters/outbound` | Único lugar con httpx/SQL/SMTP/LLM, timeouts propios, normalizar `codigo→code` | Decidir reglas de negocio |
| `ports` | Interfaces mínimas que el caso necesita | Implementaciones |
| `infrastructure` | Settings por env, DI, `main.py`, worker schedule, prompts/evals | Lógica de negocio |

## 10. Ejemplo: dar de alta un endpoint + caso nuevo

Supón `GET /catalogo/similares?sku=` (SPEC-10 similares):

1. Define el contrato en `src/ports/outbound/` si necesita un dato nuevo (o reutiliza `producto_repository_port.py`).
2. Implementa la regla en `src/domain/services/` (p. ej. criterio de similitud) sin I/O.
3. Crea/orquesta en `src/application/use_cases/gestionar_catalogo_use_case.py` (inyecta el puerto, no el adapter).
4. Expón en `src/adapters/inbound/http/catalogo_router.py` con DTO Pydantic y `dependencies/`.
5. Implementa el adapter en `src/adapters/outbound/http_clients/` con su timeout y URL de `settings.py`.
6. Cablea en `src/infrastructure/config/container.py` y registra el router en `main.py`.
7. Añade test en `tests/unit` (regla) + `tests/integration` (router con respx).
8. Actualiza este `.md` y `ARQUITECTURA-ICEPANEL.md` si aparece un componente nuevo.

Checklist: ¿el caso importa algo de `adapters` o `infrastructure`? Debe responder no. ¿el router tiene `if` de negocio? Debe responder no.

## 11. Mapeo C4

| Nivel | Carpetas |
|---|---|
| L2 App `Backend API` | `adapters/inbound/, application/, domain/, ports/, infrastructure/config, infrastructure/main.py` |
| L2 App `Worker` | `infrastructure/worker/` |
| L2 Store `DB` | `adapters/outbound/persistence/, infrastructure/db/` |
| L3a Motor | `chatbot_router, chatbot_ws_adapter, ChatbotServicePort, gestionar_conversacion, interpretar_responder, tools/, sensitive/output/degraded, llm_provider, conversacion_postgres, prompts/evals` |
| L3b Comercio | 11 routers recurso + `gestionar_sesion/catalogo/carrito/checkout/pedido/seguimiento/postventa/evidencias`, simuladores, 8 clientes + service token, repos+outbox |
| L4 | Link a cada ruta `src/...` de este archivo |

## 12. Glosario + errores comunes

BFF: el frontend solo habla con este backend; los tokens de servicio nunca van al navegador. Outbox: escribir el efecto + la tarea pendiente en la misma transacción para reintentar sin perder pagos/correos. Idempotencia: `Idempotency-Key` evita pedidos/pagos duplicados ante reintentos. JWKS: validación local del JWT sin llamar a Seguridad en cada request. `conversacion_state_port` está pendiente de renombrar a `*_repository_port` por consistencia. Errores típicos: poner `httpx` en el caso, validar rol CLIENTE en el router en vez de `jwt_validator`, guardar PAN/CVV (prohibido, solo marca+ult4), llamar al LLM con datos sin pasar por `sensitive_data_filter`.

## 13. Trazabilidad y gaps

SPEC-01..04 → `sesion/contacto routers, gestionar_sesion, jwt/rate/guard, seguridad/introspeccion clients, sms simulado, celular_verificacion model`. 

SPEC-05 → motor completo. 

SPEC-06..09 → `catalogo router, gestionar_catalogo, productos/inventario/evaluacion clients`. 

SPEC-10,11,13 → `carrito router, gestionar_carrito, inventario/cupones clients, carrito/item models`. 

SPEC-12,14 → `envio/checkout routers, gestionar_checkout, despacho client, checkout/intento models, payment simulator`. 

SPEC-15,16 → `pedidos router, gestionar_pedido, ventas client, pedido/notificacion/outbox models, worker+smtp`. 

SPEC-17,18 → `pedidos router, gestionar_pedido/seguimiento, ventas/despacho clients, service token`. 

SPEC-19..22 → `reclamos/evidencias/devoluciones routers, gestionar_postventa/evidencias, ventas client, reclamo/devolucion/evidencia models`.

Gaps fase 1: rellenar `settings/container/connection/main`, `alembic` inicial 13 tablas, 1 test por escenario (`unit/integration/evals`), cerrar A4,A5,A6,A7,A11,A12. Deuda: `conversacion_state_port` renombrar a `*_repository_port`.
