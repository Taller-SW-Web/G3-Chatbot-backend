# H3 — Motor y herramientas (Alonso)

Fuente funcional: SPEC-01, SPEC-05, SPEC-06, SPEC-09, SPEC-10 y SPEC-11 de `G3-Chatbot-specs/openspec/specs/`.

## Contratos para conectar

`build_tool_registry(handlers)` registra `solicitar_registro` siempre (ya devuelve el texto y el formulario) y solo los handlers comerciales recibidos en el mapeo nombre → handler (puede estar vacío). Los nombres soportados son `buscar_productos`, `ver_detalle_producto`, `consultar_disponibilidad`, `agregar_al_carrito`, `ver_carrito`, `cambiar_cantidad`, `quitar_del_carrito` y `vaciar_carrito`. Un nombre desconocido o un handler no invocable lanza `ValueError`. Los esquemas y descripciones de cada herramienta viven junto al constructor (`catalog_tools.py`): quien integra un módulo solo aporta el handler y lo agrega al mapeo de `provide_tool_executor()` en el contenedor.

`RegistryToolAdapter` (`src/adapters/outbound/tools/registry_tool_adapter.py`) implementa `ToolExecutorPort` sobre el `ToolRegistry` y es lo que usa el bucle del agente:

- `definitions()` traduce `registry.catalogo()` a `ToolDefinition` del proveedor LLM (`nombre`, `descripcion`, `parametros`).
- `execute()` convierte `ToolExecutionContext` en `ExecutionContext` (UUID a texto, `anonymous_sid` a `chat_sid`, `None` se conserva) y llama a `registry.ejecutar`, que nunca confirma acciones.
- `is_error=True` solo para los rechazos del registro: `HERRAMIENTA_DESCONOCIDA`, `ARGUMENTOS_INVALIDOS`, `REQUIERE_SESION` y `REQUIERE_CONFIRMACION`. Los códigos de los handlers pasan sin marcar error.
- Los bloques de UI que el handler deja en `data["bloques"]` se extraen a `ToolResult.blocks` (el puerto transporta una secuencia de bloques y el caso de uso emite un `BlockEvent` por bloque) y se quitan de `data`, de modo que el modelo no recibe payloads de UI.
- Las excepciones de los handlers se propagan; el caso de uso las convierte en `ERROR_INTERNO`.

Cada handler recibe `(modelo_pydantic, ExecutionContext)` y devuelve `ToolResult(code, data)`. El adaptador de catálogo y carrito debe resolver ordinales y variantes, verificar pertenencia, consultar stock, recalcular precios y producir bloques con datos reales. No se deben registrar resultados comerciales simulados. `product_id` identifica el producto de integración; `productoRef` es el ordinal 1–10 del carrusel de esa conversación. `itemId`, `itemRef` y `nombre` son alternativas para resolver una línea del carrito. Cambiar cantidad a cero delega la eliminación.

El orquestador puede obtener las definiciones con `registry.catalogo()` y ejecutar `await registry.ejecutar(nombre, argumentos, contexto)`. El registro valida argumentos con Pydantic y devuelve `ARGUMENTOS_INVALIDOS` sin repetir valores recibidos. El límite de cinco iteraciones y el único reintento de validación pertenecen al caso de uso. Los errores de handlers no se reintentan para evitar repetir mutaciones.

El servidor construye `ExecutionContext` después de validar identidad y pertenencia; descarta cualquier identidad propuesta por el LLM. Una herramienta protegida devuelve `REQUIERE_SESION` con `accionPendiente`. El `POST /chat/conversaciones/{id}/mensajes` también acepta `{ accion: { tipo, payload } }`: el dispatcher usa los handlers del registro construidos con la misma sesión, no invoca el LLM ni abre SSE, comprueba pertenencia y guarda filas TOOL/ASSISTANT en una transacción. La respuesta REST tiene `{ code, mensajeId, texto, bloques, datos }`. La confirmación de vaciar el carrito se mantiene denegada porque aún no existe estado persistido para validar una confirmación pendiente del servidor.

## Seguridad y degradación

- `SensitiveDataFilter.redactar(texto, otp_solicitado=...)` reemplaza tarjetas válidas por Luhn, documentos con formato reconocido y contraseñas asignadas antes de persistir el mensaje, derivar el título o construir el contexto del LLM. El contenedor conecta el filtro al caso de uso. La etiqueta por sí sola no basta para ocultar una contraseña: preguntas como «¿cómo cambio mi contraseña?» se conservan. El flujo debe pasar `otp_solicitado=True` solo cuando el estado de la conversación espera un OTP; las imágenes y direcciones libres no se procesan (ADR-0006).
- `OutputValidator.validar(texto, montos=..., porcentajes=...)` corre antes de persistir la respuesta final. Solo resultados de herramientas con `code == "OK"` autorizan importes y porcentajes numericos con claves comerciales allowlist; nunca se acepta evidencia del LLM ni de herramientas fallidas. Sustituye menciones no respaldadas. `fin` conserva `{mensajeId}` por ADR-0021; el cliente debe recargar el historial al cerrar para reemplazar el borrador SSE por el texto validado. No verifica que precio corresponde a que producto ni afirmaciones de stock/estado: esos datos deben renderizarse desde bloques.
- `DegradedMode.responder(texto_redactado, fallo_imagen=...)` se usa cuando falla el LLM. Si detecta una búsqueda segura por categoría y `buscar_productos` está registrada, ejecuta solo esa herramienta de lectura. El menú muestra solo acciones que el registro puede despachar; si ninguna tiene handler, el turno responde sin emitir un bloque de acciones vacío. No ejecuta compras ni mutaciones. `activo` depende de la disponibilidad del LLM; el transporte del chat es SSE en la respuesta del `POST`, con recuperación por polling si se corta el stream (ADR-0021).

El menú canónico define seis opciones. En este PR, el dispatcher puede ofrecer `BUSCAR_POR_CATEGORIA` y `VER_CARRITO` cuando sus handlers están registrados. `VER_OFERTAS`, `MIS_PEDIDOS`, `MIS_RECLAMOS` y `MIS_DEVOLUCIONES` siguen sin handler y no se muestran; dependen de las capacidades de promociones, pedidos, reclamos y devoluciones.

## Verificación y límites de integración

Desde la raíz del backend, activa el entorno virtual y ejecuta:

```text
python -m pytest -q
git diff --check
```

Las pruebas de integración con PostgreSQL requieren `TEST_DATABASE_URL`. El contenedor construye los handlers comerciales de catálogo y carrito con la sesión de la petición; la integración ampliada de carrito y stock del PR #46 queda pendiente. El flujo REST, los guardarraíles de salida y el modo degradado están conectados.
