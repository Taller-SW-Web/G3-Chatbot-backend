# H3 — Motor y herramientas (Alonso)

Fuente: SPEC-01, 05, 06, 09, 10 y 11 en `../G3-Chatbot-specs/openspec/specs/`.
La rama local `feature/h3-motor-herramientas` está actualizada sobre `development` en `7095486`.

## Contratos para conectar

`crear_registro_h3(handlers)` exige ocho handlers async, uno por herramienta:
`buscar_productos`, `ver_detalle_producto`, `consultar_disponibilidad`,
`agregar_al_carrito`, `ver_carrito`, `cambiar_cantidad`, `quitar_del_carrito`
y `vaciar_carrito`. `solicitar_registro` ya muestra texto y formulario.

Cada handler recibe `(modelo_pydantic, ExecutionContext)` y devuelve
`ToolResult(code, data)`. Sonny conecta catálogo/carrito: normalización, resolución
de ordinales/nombres/variantes, pertenencia de items, stock vivo, límites acumulados,
recalcular precios y bloques. No devolver éxitos simulados al cablear producción.
`product_id` es el identificador de integración; `productoRef` es un ordinal 1–10
del carrusel de esa conversación. `itemId`, `itemRef` y `nombre` son alternativas
para resolver una línea del carrito. Cambiar cantidad a cero delega su eliminación.

Mathias utiliza `registry.catalogo()` para generar tools del proveedor y
`await registry.ejecutar(nombre, argumentos, contexto)` en el ciclo del LLM.
El registro valida Pydantic; devuelve `ARGUMENTOS_INVALIDOS` con campos/tipos,
sin valores recibidos. El único reintento de validación y límite de 5 iteraciones
pertenecen al orquestador. Errores de handlers se propagan, sin reintentar mutaciones.

El servidor construye `ExecutionContext` después de validar JWT, `chat_sid` y
pertenencia. Identidad propuesta por LLM se descarta. Una herramienta protegida
devuelve `REQUIERE_SESION` con `accionPendiente`; el caso de uso la persiste y
presenta login. Nuevas herramientas se registran con sus banderas de sesión
y confirmación según SPEC-05; los ocho handlers H3 permiten uso anónimo.

`ActionDispatcher.despachar({tipo, payload}, contexto)` ejecuta el mismo handler
sin LLM/WS. Para vaciar devuelve `REQUIERE_CONFIRMACION`; el router habilita
`confirmacion_ui=True` únicamente tras validar confirmación explícita de la UI
para la acción pendiente y su pertenencia. No copiar una bandera del payload.
El caso de uso registra el historial HERRAMIENTA y controla commit/rollback.

## Guardarraíles y fallback

- `SensitiveDataFilter.redactar(texto, otp_solicitado=...)`: usar el texto devuelto
  antes de persistir, armar el prompt, derivar título o registrar logs. OTP depende
  del estado de esa conversación. Devuelve categorías y avisos sin originales;
  con DOCUMENTO el orquestador añade «Ir al pago». No detecta direcciones libres
  ni secretos sin etiqueta; no procesa imágenes (ADR-0006).
- `OutputValidator.validar(texto, montos=..., porcentajes=...)`: recibe Decimal
  autorizados de tools, nunca del LLM. Sustituye importes y porcentajes de descuento
  no respaldados, conservando porcentajes de composición textil;
  usar `resultado.texto` en `fin` cuando `corregido` sea verdadero. No verifica
  semánticamente qué precio corresponde a qué producto ni todas las afirmaciones
  de stock/estado: estos datos se renderizan desde bloques de las herramientas.
- `DegradedMode.responder(texto_redactado, fallo_imagen=...)`: devuelve menú o
  plan `buscar_productos` + consulta por palabras clave. El orquestador ejecuta
  el plan por el registro y responde por REST. No ejecuta pagos ni mutaciones.
  Si solo falla WS, el LLM puede continuar y entregar texto completo por polling.
  El menú canónico incluye ofertas y consultas de H4; requieren sus handlers y
  mappings UI cuando el equipo implemente esas capacidades.

## Verificación y pendiente

`.venv/Scripts/python.exe -m pytest -q -W error` desde la raíz del backend: 228 aprobadas y 13 omitidas por falta de `TEST_DATABASE_URL`. OpenSpec strict: cambio H3 válido y 23 specs aprobadas.
Las pruebas nuevas verifican estos contratos con dobles de handlers y servicios
puros. El cableado con casos de uso reales, autenticación, historial y evento `fin`
queda pendiente de Mathias/Sonny. El cambio OpenSpec se mantiene abierto por ello.
