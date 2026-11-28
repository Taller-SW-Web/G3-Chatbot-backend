# `mod-chat` — Chat

- **Owner:** @Taller-SW-Web/chatbot
- **Backend:** paquete `mod_chat`, `basePath` `/chat/conversaciones`
- **Frontend:** carpeta `mod-chat`, rutas `[/chat/[id]]`, `[/sugerencias]`
- **Depende de:** todos los módulos, solo por su `public.py`
  (`mod-auth`, `mod-catalog`, `mod-cart`, `mod-shipping`, `mod-checkout`,
  `mod-orders`, `mod-postventa`)
- **Contrato:** `docs/openapi.json`, tag `mod-chat`.

## Responsabilidad

Conversaciones, historial, búsqueda, turnos con streaming SSE y el bucle
LLM → herramienta → LLM con el catálogo de tools y las acciones directas de la
UI. **No** implementa reglas de catálogo, carrito, checkout ni postventa: las
invoca por sus casos de uso públicos.

## Superficie pública

| Tipo | Archivo | Qué expone |
|---|---|---|
| Backend | `public.py` | `provide_chatbot_service`, `provide_turn_scope`, `provide_action_scope`, `provide_tool_registry`, `InterpretAndRespondUseCase`, `DispatchDirectActionUseCase`, `QUICK_ACTIONS` |
| Frontend | `public.ts` | Tipos de evento SSE, mensaje y bloque |

Ningún módulo importa `mod-chat` (es la cima del DAG). El composition root usa
`ACTION_SCOPE` para montar el scope de acciones directas en producción. Chat
registra además `conversation_context` (el puerto que cart/catalog usan para la
memoria de conversación y la auditoría TOOL sin importar la persistencia de
chat) y `conversation_retention` (job periódico `CONVERSATION_RETENTION_INTERVAL_SECONDS`
que cierra conversaciones anónimas >7 días y archiva conversaciones de cliente
>90 días, ejecutado desde `outbox_worker_lifespan`).

## Rutas

| Método | Ruta | Caso de uso |
|---|---|---|
| POST | `/api/v1/chat/conversaciones` | `ManageConversationUseCase.create_conversation` |
| GET | `/api/v1/chat/conversaciones` | `ManageConversationUseCase.list_conversations` |
| GET | `/api/v1/chat/conversaciones/buscar` | `ManageConversationUseCase.search_conversations` |
| GET | `/api/v1/chat/conversaciones/{conversation_id}/mensajes` | `ManageConversationUseCase.get_history` |
| POST | `/api/v1/chat/conversaciones/{conversation_id}/mensajes` | `InterpretAndRespondUseCase.send_message` (SSE) o `DispatchDirectActionUseCase.execute` (acción directa) |
| POST | `/api/v1/chat/conversaciones/{id}/adjuntos` | `AttachmentService.upload` (multipart, campo `archivo`) |
| DELETE | `/api/v1/chat/conversaciones/{id}/adjuntos/{adjuntoId}` | `AttachmentService.delete` |
| GET | `/api/v1/chat/adjuntos/{adjuntoId}` | `AttachmentService.signed_urls` |
| GET | `/api/v1/chat/adjuntos/archivo` | lectura del objeto con URL firmada |

## Adjuntos (SPEC-23)

- Hasta 3 imágenes por mensaje y 10 `PENDING` por conversación; `image/jpeg`,
  `image/png` e `image/webp`, máx. 5 MB.
- El tipo se detecta por firma binaria, se elimina EXIF/GPS, se acota a 2048 px
  y se genera una miniatura de 320 px (`PillowImageProcessor`).
- `AttachmentStorage` es un puerto con adaptador local privado
  (`ATTACHMENT_STORAGE_PATH`); las URLs firmadas caducan
  (`ATTACHMENT_SIGNED_URL_TTL_SECONDS`) y el archivo se sirve por
  `/chat/adjuntos/archivo`.
- `adjuntoIds` en `POST .../mensajes` valida pertenencia y estado `PENDING`,
  liga los adjuntos al mensaje y los pasa al LLM como `ImagePart` base64 (máx.
  6 imágenes por turno; nunca URLs). Sin visión, el texto sigue funcionando y
  un mensaje solo-imagen degrada con el aviso correspondiente.
- Un job horario borra `PENDING` de más de 24 h y archivos huérfanos.

## Retroalimentación (SPEC-24)

| Método | Ruta | Caso de uso |
|---|---|---|
| POST | `/api/v1/mensajes/{mensajeId}/reaccion` | `FeedbackService.react` (👍/👎 idempotente) |
| POST | `/api/v1/sugerencias` | `FeedbackService.create_suggestion` (multipart, captura opcional) |

- Una reacción vigente por mensaje y por cliente/`chat_sid`; 30/min y el exceso
  se ignora en silencio (204).
- Sugerencias/bugs: título 3–120, descripción 6–1000, correo válido, captura
  JPG/PNG opcional en el bucket privado (`feedback/...`); 5/hora y
  `Idempotency-Key` obligatoria.
- La notificación interna se encola como `SEND_FEEDBACK_EMAIL` y reutiliza el
  `EmailSender` de SPEC-16; si falla, el outbox reintenta sin perder el registro.

## Catálogo M5 (SPEC-05)

- El `ToolRegistry` publica exactamente las 29 herramientas de
  `shared/application/tools/tool_contracts.py`; no hay alias de
  `listar_pedidos`/`consultar_pedido`.
- `confirmar_reclamo` y `confirmar_devolucion` son acciones **solo UI**
  (`solo_ui=True`): el dispatcher las ejecuta con `confirmacion_ui=True` tras el
  botón "Enviar reclamo"/"Enviar solicitud", pero nunca se ofrecen al LLM.
  El submit envía `payload.confirmado: true`; `DispatchDirectActionUseCase` lo
  consume y lo retira antes de validar los argumentos.
- `iniciar_checkout`, `cerrar_sesion`, `nueva_conversacion` y
  `buscar_conversaciones` devuelven acciones de cliente (`IR_A_CHECKOUT`,
  `CERRAR_SESION`, `NUEVO_CHAT`, `BUSCAR_CHATS`); no crean checkout, no tocan
  cookies y no crean conversaciones.
- `consultar_pedidos` aplica D7: sin `pedidoId` ni `estado`, solo muestra
  `ESTADO_PEDIDO` si hay exactamente un pedido no finalizado.
- `consultar_seguimiento` añade `datos.seguimiento` (lista blanca de Despacho)
  junto a `datos.hitos` de Ventas.

## Dependencias externas

- **LLM** (`LLM_PROVIDER=openai`) vía `ports/llm_provider_port.py`; sin API key
  el turno degrada (`DegradedMode`) en lugar de fallar. `LLM_PROVIDER=fake`
  (`ScriptedLLMProvider`) responde con un guion fijo sin red, solo para las
  pruebas de carga (H5-P2); se rechaza con `APP_ENV=production`.
- Herramientas de catálogo, carrito, pedidos y postventa, construidas sobre los
  casos de uso públicos de cada módulo.
- Memoria de conversación y mensajes: adaptadores compartidos de `src/shared`;
  cart/catalog los consumen solo a través de `ConversationContextPort`.

## Pruebas

- Unitarias: `tests/unit/mod_chat/`
- Integración: `tests/integration/mod_chat/`
- Contrato: eventos SSE ↔ `public.ts` y `docs/openapi.json` (tag `mod-chat`)
- Evals H5-E1: `python -m pytest tests/evals -m evals -q` con `LLM_API_KEY`
  (y opcionalmente `LLM_MODEL`, por defecto `gpt-6-luna`) exportados. Sin clave,
  los casos reales se omiten y solo corre el modo degradado, el guardarraíl y el
  humo del harness. Un caso real que cae al modo degradado cuenta como fallo.

## Reglas

- No importar `domain/`, `application/` ni `adapters/` de otro módulo.
- No tocar `shared/` sin label `cross-modulo`.
- Un PR de este módulo no modifica archivos de otro `mod-*`.
