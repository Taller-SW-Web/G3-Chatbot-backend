# H3 — Motor y herramientas (Alonso)

Fuente funcional: SPEC-01, SPEC-05, SPEC-06, SPEC-09, SPEC-10 y SPEC-11 de `G3-Chatbot-specs/openspec/specs/`.

## Contratos para conectar

`build_tool_registry(handlers)` recibe ocho handlers asíncronos: `buscar_productos`, `ver_detalle_producto`, `consultar_disponibilidad`, `agregar_al_carrito`, `ver_carrito`, `cambiar_cantidad`, `quitar_del_carrito` y `vaciar_carrito`. `solicitar_registro` ya devuelve el texto y el formulario.

Cada handler recibe `(modelo_pydantic, ExecutionContext)` y devuelve `ToolResult(code, data)`. El adaptador de catálogo y carrito debe resolver ordinales y variantes, verificar pertenencia, consultar stock, recalcular precios y producir bloques con datos reales. No se deben registrar resultados comerciales simulados. `product_id` identifica el producto de integración; `productoRef` es el ordinal 1–10 del carrusel de esa conversación. `itemId`, `itemRef` y `nombre` son alternativas para resolver una línea del carrito. Cambiar cantidad a cero delega la eliminación.

El orquestador puede obtener las definiciones con `registry.catalogo()` y ejecutar `await registry.ejecutar(nombre, argumentos, contexto)`. El registro valida argumentos con Pydantic y devuelve `ARGUMENTOS_INVALIDOS` sin repetir valores recibidos. El límite de cinco iteraciones y el único reintento de validación pertenecen al caso de uso. Los errores de handlers no se reintentan para evitar repetir mutaciones.

El servidor construye `ExecutionContext` después de validar identidad y pertenencia; descarta cualquier identidad propuesta por el LLM. Una herramienta protegida devuelve `REQUIERE_SESION` con `accionPendiente`. Para vaciar el carrito, el router habilita `confirmacion_ui=True` solo tras validar la confirmación explícita y la pertenencia de la acción pendiente. El caso de uso registra el historial de herramientas y controla commit/rollback.

## Seguridad y degradación

- `SensitiveDataFilter.redactar(texto, otp_solicitado=...)` reemplaza tarjetas válidas por Luhn, documentos con formato reconocido y contraseñas asignadas antes de persistir el mensaje, derivar el título o construir el contexto del LLM. El contenedor conecta el filtro al caso de uso. La etiqueta por sí sola no basta para ocultar una contraseña: preguntas como «¿cómo cambio mi contraseña?» se conservan. El flujo debe pasar `otp_solicitado=True` solo cuando el estado de la conversación espera un OTP; las imágenes y direcciones libres no se procesan (ADR-0006).
- `OutputValidator.validar(texto, montos=..., porcentajes=...)` compara menciones monetarias en PEN y descuentos con valores `Decimal` autorizados por herramientas, nunca con listas del LLM. Sustituye valores no respaldados; conserva porcentajes de características textiles. Aún no está conectado al evento `fin` hasta completar la integración con las herramientas reales. No verifica qué precio corresponde a qué producto ni afirmaciones de stock/estado; esos datos deben renderizarse desde los bloques de las herramientas.
- `DegradedMode.responder(texto_redactado, fallo_imagen=...)` devuelve el menú o una búsqueda de solo lectura con las palabras clave de categoría detectadas. No ejecuta compras ni mutaciones. `activo` depende de la disponibilidad del LLM; el transporte del chat es SSE en la respuesta del `POST`, con recuperación por polling si se corta el stream (ADR-0021).

El menú canónico incluye ofertas y consultas de H4; sus handlers y mappings de UI se completarán cuando el equipo implemente esas capacidades.

## Verificación y límites de integración

Desde la raíz del backend, activa el entorno virtual y ejecuta:

```text
python -m pytest -q
git diff --check
```

Las pruebas de integración con PostgreSQL requieren `TEST_DATABASE_URL`. Esta rama entrega los contratos, validadores y controles de seguridad; la integración de los handlers comerciales con los casos de uso, autenticación, historial y bloques queda coordinada con el resto del Hito 3.
