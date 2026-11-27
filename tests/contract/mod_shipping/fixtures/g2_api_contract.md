# Contrato de API: integraciones externas y convenciones compartidas

## 1. Propósito

Este documento define las comunicaciones de Despacho y Entrega con Marketplace, Chatbot, Ventas y Postventa, Productos y Ofertas y Seguridad y Usuarios. Los contratos se distribuyen en tres archivos, con una sola definición de cada operación:

| Documento | Alcance y lectores |
|---|---|
| [Integraciones externas](./api-contract.md) | Equipos de otros módulos: operaciones, autenticación, eventos y acuerdos de integración. También conserva las convenciones comunes (§3) y la idempotencia compartida (§9). |
| [Gestión de Despachos](./api-contract-gestion-despachos.md) | Frontend administrativo y backend de F-01, F-02 y F-04; catálogo común y API interna provista por Gestión. |
| [Operación de Reparto y Flota](./api-contract-operacion-reparto.md) | Frontend del repartidor y administración de F-05/F-06; evidencias, jornadas, capacidad y API interna provista por Operación. |

Cada contrato de microservicio enlaza las integraciones externas que implementa o consume. Las convenciones se referencian, sin mantener copias; los endpoints internos se documentan en el contrato de su proveedor. Esta organización cambia la ubicación de la documentación, no las rutas, payloads ni reglas acordadas.

Este contrato establece:

1. Despacho es responsable de calcular la cotización.
2. Marketplace, Chatbot o Ventas envían destino y líneas con `sku` y `cantidad`; no calculan peso, volumen ni tarifa.
3. Para cotizar, Despacho consulta en lote a Productos y Ofertas los datos físicos vigentes de cada SKU.
4. La cotización se protege con un token de servicio y el scope `cotizaciones:calcular`. No requiere un JWT de usuario ni una API key adicional.
5. El seguimiento para canales se unifica por `idPedido`, exige un token de servicio con el scope `seguimientos:leer` y no expone coordenadas ni datos personales.
6. Ventas y Postventa solicita el despacho solamente después de que el pedido esté pagado, preparado y listo para entrega. La solicitud contiene las líneas del pedido (`sku` y `cantidad`); Despacho obtiene de Productos los datos físicos y calcula el peso y volumen operativos.
7. Cada `idPedido` puede originar como máximo un despacho.
8. Despacho no emite credenciales. Seguridad y Usuarios emite los JWT de usuario y los tokens de servicio OAuth 2.0 mediante `client_credentials`.
9. Gestión de Despachos es la única fuente del estado canónico del despacho.
10. Los cambios de estado se comunican a Ventas y Postventa de manera asíncrona e idempotente.
11. Para el alcance inicial, cada pedido representa un despacho y un paquete lógico; `cantidadPaquetes` se registra internamente con valor `1` y no forma parte de la solicitud de Ventas.

## 2. Límites y responsabilidades

| Participante | Es dueño de | Envía a Despacho | Recibe de Despacho |
|---|---|---|---|
| Marketplace | Experiencia del canal | Destino, SKU y cantidad; `idPedido` para seguimiento | Cobertura, cotización, plazo y estado resumido |
| Chatbot | Conversación con el cliente | Destino, SKU y cantidad; `idPedido` para seguimiento | Cobertura, cotización, plazo y estado resumido |
| Ventas y Postventa | Pedido, pago, preparación, anulaciones y tratamiento comercial | Pedido listo para entrega con destinatario, destino y líneas (`sku`, `cantidad`); cancelaciones | Identificador del despacho y eventos de estado |
| Productos y Ofertas | Catálogo, SKU, peso y dimensiones del producto | Datos físicos vigentes consultados por Despacho | Consulta de SKUs realizada por Despacho |
| Seguridad y Usuarios | Identidades, credenciales y roles globales | JWT de usuario, tokens de servicio, JWKS e identificadores de usuario | Ninguna credencial; el alta global la realiza `ADMIN_SISTEMA` y Despacho conserva únicamente el vínculo local con `usuario_id` |
| Gestión de Despachos | Zonas, tarifas, despacho, estado e historial | Comandos y estados confirmados para Operación | Disponibilidad, capacidad y transiciones solicitadas |
| Operación de Reparto y Flota | Repartidores, furgonetas, jornadas, capacidad y evidencias | Disponibilidad y comandos de transición de campo | Asignaciones y estados confirmados |

Despacho no administra pagos, stock, embalaje, cuentas de usuario, navegación GPS, guías de remisión, reembolsos ni notificaciones al cliente final. Una solicitud autenticada de Ventas declara que el pedido ya cumple las condiciones comerciales y de preparación; Despacho no consulta el estado interno del pedido ni modifica sus existencias.

## 3. Convenciones comunes

### 3.1. URL y formato

- Prefijo público: `/api/v1`.
- Prefijo entre microservicios: `/internal/v1`.
- Formato: `application/json; charset=UTF-8`.
- Campos JSON: `camelCase`.
- Enumeraciones: `UPPER_SNAKE_CASE`.
- Fechas y horas: ISO 8601 en UTC.
- Importes: número decimal y moneda ISO 4217.
- Identificadores: cadenas opacas; los clientes no deben inferir información desde ellas.

### 3.2. Encabezados

| Encabezado | Uso |
|---|---|
| `Authorization: Bearer <token>` | Operaciones con JWT de usuario o token de servicio |
| `Idempotency-Key: <uuid>` | Reintentos seguros en los comandos que lo declaren; es obligatorio como UUID v4 de hasta 128 caracteres al crear o cancelar despachos desde Ventas |
| `X-Correlation-Id: <uuid>` | Opcional; trazabilidad entre módulos. El Gateway lo genera si no llega |
| `Content-Type: application/json` | Cuerpos JSON |

La obtención de un token de servicio es la excepción al último encabezado: `POST /api/v1/auth/token` utiliza `Content-Type: application/x-www-form-urlencoded`, conforme al contrato de Seguridad y Usuarios.

### 3.3. Roles humanos y tokens de servicio

| Rol global | Uso |
|---|---|
| `GESTOR_DESPACHO` | Todas las operaciones administrativas: zonas, tarifas, programación, asignación, incidencias, repartidores, furgonetas y jornadas |

`REPARTIDOR` no forma parte del catálogo global de roles de Seguridad. Es un perfil local de Despacho enlazado al `sub` del usuario mediante el `usuario_id` almacenado por F-05. Las operaciones de campo se autorizan verificando que ese perfil exista, esté activo y sea propietario de la ruta o del recurso solicitado.

Los módulos externos no utilizan un rol humano `SERVICIO_INTEGRACION`. Seguridad emite tokens de servicio en los que:

- `sub` contiene el `client_id`, por ejemplo `modulo-chatbot` o `modulo-ventas`.
- `tipo` vale `servicio`.
- `scope` contiene los permisos concedidos al cliente.
- `aud` contiene las APIs dueñas de esos scopes, por ejemplo `api-despacho`.
- `iss` identifica al emisor de Seguridad.
- `iat`, `exp` y `jti` permiten validar emisión, vencimiento y unicidad del token.

Los tokens de usuarios humanos llevan `tipo=acceso`, un UUID de usuario en `sub` y sus roles en `roles`. Los tokens de refresco no se aceptan en las APIs de negocio.

El valor de `iss` se obtiene del campo `issuer` publicado por `GET /api/v1/auth/.well-known/openid-configuration`; no se fija manualmente porque cambia según el entorno. En el JWT de servicio, `scope` es un texto con valores separados por espacios y `aud` identifica las APIs destinatarias. El token de servicio dura una hora y no tiene token de refresco. Despacho exige `aud=api-despacho` en sus endpoints de integración. El contrato actual de Seguridad no garantiza `aud` en los tokens humanos de acceso, por lo que esa validación se aplica únicamente a los tokens de servicio.

### 3.4. Scopes de servicio de Despacho

| Scope | Consumidores previstos | Operación autorizada |
|---|---|---|
| `cotizaciones:calcular` | Marketplace y Chatbot | Calcular cobertura, costo y plazo |
| `seguimientos:leer` | Marketplace, Chatbot y Ventas | Consultar seguimiento por `idPedido` |
| `despachos:crear` | Ventas | Crear un despacho para un pedido pagado, preparado y listo para entrega |
| `despachos:cancelar` | Ventas | Solicitar cancelación por anulación del pedido |

Estos scopes pertenecen a la API de Despacho: Despacho define su significado y Seguridad los registra y concede a cada `client_id`. En cambio, `usuarios:leer` y `direcciones:leer` pertenecen a la API de Seguridad y ya están concedidos a `modulo-despacho`. Para consultar datos físicos, Productos y Ofertas acordó el scope `productos:fisicos:leer`, perteneciente a `api-productos`, que deberá concederse al cliente técnico `modulo-despacho`.

La integración con Ventas utiliza permisos distintos según la dirección de la llamada. `modulo-ventas` llama a `api-despacho` con `despachos:crear`, `despachos:cancelar` o `seguimientos:leer`. En la dirección contraria, `modulo-despacho` llama al webhook de `api-ventas` con `ventas:pedidos:estado`. No se utiliza el scope propuesto inicialmente `despachos:eventos:recibir`, porque Ventas confirmó la reutilización de `ventas:pedidos:estado`.

### 3.5. Errores

Las respuestas de error usan `Content-Type: application/problem+json`, siguiendo el mismo enfoque RFC 7807 adoptado por Seguridad y Usuarios. Los consumidores deben tomar decisiones por `status` y `code`, no comparando el texto de `detail`.

Los errores públicos `DESP_ERROR_PRODUCTO_NO_ENCONTRADO`, `DESP_ERROR_DATOS_FISICOS_INCOMPLETOS` y `DESP_ERROR_PRODUCTO_NO_ELEGIBLE` identifican los SKU causantes mediante `details.skusAfectados`: arreglo no vacío, de hasta 100 cadenas no vacías, sin duplicados y pertenecientes a la solicitud. Se conservan literalmente sus caracteres, mayúsculas y ceros iniciales. G2 conserva la identificación válida del proveedor o la obtiene de la respuesta íntegra que originó su propia validación; no atribuye el error a todos los SKU del lote por defecto. `correlationId` permite rastrear la petición y complementa esa identificación.

Esta extensión del error público de G2 no obliga a todos los problemas HTTP a incluir `details`. En G6, el campo está homologado para `DATOS_FISICOS_INCOMPLETOS` y, por el acuerdo D-G2-02 cerrado el 04/11/2026, para el rechazo por SKU padre con variantes de §5.2.4. La sincronización de esta ampliación con el OpenAPI del proveedor se sigue en §11.1. Las respuestas de error no contienen resultados físicos parciales.

```json
{
  "type": "https://despacho.example.com/problemas/sin-cobertura",
  "title": "Destino sin cobertura",
  "status": 422,
  "code": "DESP_ERROR_SIN_COBERTURA",
  "detail": "El destino no pertenece a una zona de cobertura activa",
  "instance": "/api/v1/cotizaciones",
  "errores": [
    {
      "campo": "destino.distrito",
      "motivo": "Distrito no cubierto"
    }
  ],
  "correlationId": "35ac19fa-4b25-4a98-bbdd-882234ec1a2c",
  "timestamp": "2026-09-23T18:30:00Z"
}
```

| Código HTTP | Significado |
|---|---|
| `400 Bad Request` | Formato o validación básica inválida |
| `401 Unauthorized` | Token ausente, firma inválida, token vencido o claims de seguridad no aceptados; para tokens de servicio incluye `iss`, `aud` y `tipo` incorrectos |
| `403 Forbidden` | Token válido, pero sin el scope, rol o propiedad del recurso requerido |
| `404 Not Found` | Recurso inexistente |
| `409 Conflict` | Estado incompatible, duplicado o modificación concurrente |
| `422 Unprocessable Entity` | Solicitud válida, pero no procesable por una regla del negocio |
| `429 Too Many Requests` | Límite de cotizaciones excedido |
| `500 Internal Server Error` | Error interno que impide completar la operación, incluida la persistencia obligatoria de una cotización |
| `502 Bad Gateway` | Respuesta de una dependencia que incumple el contrato esperado |
| `503 Service Unavailable` | Dependencia necesaria temporalmente no disponible |

Todos los endpoints protegidos aplican esta distinción antes de ejecutar la operación de negocio. Una respuesta `401` incluye el encabezado `WWW-Authenticate: Bearer` y no indica si el recurso solicitado existe. Una respuesta `403` tampoco expone datos del pedido, despacho, destinatario o repartidor.

Ejemplo de token no aceptado:

```http
HTTP/1.1 401 Unauthorized
Content-Type: application/problem+json
WWW-Authenticate: Bearer
```

```json
{
  "type": "https://despacho.example.com/problemas/token-invalido",
  "title": "Token no aceptado",
  "status": 401,
  "code": "DESP_ERROR_TOKEN_INVALIDO",
  "detail": "No fue posible autenticar la solicitud",
  "instance": "/api/v1/seguimientos/pedidos/PED-2026-00891",
  "correlationId": "35ac19fa-4b25-4a98-bbdd-882234ec1a2c",
  "timestamp": "2026-09-23T18:30:00Z"
}
```

Ejemplo de token válido sin el scope exigido por la operación:

```http
HTTP/1.1 403 Forbidden
Content-Type: application/problem+json
```

```json
{
  "type": "https://despacho.example.com/problemas/permiso-insuficiente",
  "title": "Permiso insuficiente",
  "status": 403,
  "code": "DESP_ERROR_SCOPE_INSUFICIENTE",
  "detail": "El cliente no tiene permiso para realizar esta operación",
  "instance": "/api/v1/seguimientos/pedidos/PED-2026-00891",
  "correlationId": "35ac19fa-4b25-4a98-bbdd-882234ec1a2c",
  "timestamp": "2026-09-23T18:30:00Z"
}
```

Para cerrar la integración se deben probar, como mínimo, los casos sin token, token inválido o vencido, audiencia incorrecta, scope ausente y scope correcto. Una audiencia que no contenga `api-despacho` se rechaza con `401 Unauthorized` antes de ejecutar la operación de negocio.

## 4. Matriz de comunicaciones externas

| Origen | Destino | Comunicación | Seguridad | Mecanismo |
|---|---|---|---|---|
| Marketplace o Chatbot | Despacho | Solicitar cotización | Token de servicio con `cotizaciones:calcular` | REST síncrono |
| Marketplace o Chatbot | Despacho | Consultar seguimiento por `idPedido` | Token de servicio con `seguimientos:leer` | REST síncrono |
| Despacho | Productos y Ofertas | Consultar datos físicos por SKU | Token de `modulo-despacho` con `aud=api-productos` y scope `productos:fisicos:leer` | REST síncrono en lote |
| Ventas y Postventa | Despacho | Crear despacho de pedido listo para entrega | Token de `modulo-ventas` con `aud=api-despacho` y scope `despachos:crear` | REST síncrono e idempotente |
| Ventas y Postventa | Despacho | Cancelar por anulación | Token de `modulo-ventas` con `aud=api-despacho` y scope `despachos:cancelar` | REST síncrono e idempotente |
| Despacho | Ventas y Postventa | Informar cambios de estado | Token de `modulo-despacho` con `aud=api-ventas` y scope `ventas:pedidos:estado` | Webhook con outbox y reintentos |
| Despacho | Seguridad y Usuarios | Obtener claves públicas | HTTPS | JWKS con caché |
| Seguridad y Usuarios | Despacho | Notificar cambios de estado o roles de usuarios | Credenciales y permisos del broker pendientes de publicación | Eventos RabbitMQ previstos por Seguridad |

## 5. F-01: cotización para Marketplace y Chatbot

### 5.1. Solicitar cotización

- **Método:** `POST`
- **Ruta:** `/api/v1/cotizaciones`
- **Autenticación:** token de servicio con `tipo=servicio` y scope `cotizaciones:calcular`.
- **Identidad del consumidor:** claim `sub` del token, por ejemplo `modulo-chatbot`.
- **Límite:** configurable por `sub` e IP.
- **Idempotencia:** no requiere `Idempotency-Key`. Cada ejecución exacta exitosa conserva su propia instantánea interna; una repetición puede producir otra cotización, sin crear pedidos, despachos ni cobros y sin prometer reutilizar el precio de una ejecución anterior.

La misma operación atiende tres consultas según el cuerpo recibido:

| Cuerpo | Resultado | `tipoCotizacion` |
|---|---|---|
| Solo `destino` | Cobertura, zona y datos del destino | — |
| `destino` y `lineas` | Cobertura y costo calculado con los datos físicos de los productos | `EXACTA` |
| Destino sin cobertura | Solo cobertura y datos del destino, sin costo ni plazo | — |

Solicitud sin productos (solo cobertura):

```json
{
  "destino": {
    "direccion": "Av. Javier Prado Este 2465",
    "distrito": "San Borja",
    "codigoPostal": "15036"
  }
}
```

Solicitud con productos:

```json
{
  "destino": {
    "direccion": "Av. Javier Prado Este 2465",
    "distrito": "San Borja",
    "codigoPostal": "15036"
  },
  "lineas": [
    { "sku": "POL-NEG-M", "cantidad": 5 },
    { "sku": "ZAP-RUN-42", "cantidad": 1 }
  ]
}
```

Reglas:

- Debe informarse `distrito`; `direccion`, `codigoPostal` y los demás datos del destino son opcionales.
- `lineas` es opcional. Si se omite, la respuesta confirma únicamente la cobertura, la zona y los datos del destino, sin costo ni plazo. Si se envía, debe contener al menos un elemento.
- En cada línea, `sku` es obligatorio y `cantidad` debe ser mayor que cero.
- Despacho evalúa primero la cobertura. Si el destino no tiene cobertura, responde sin consultar a Productos ni validar las líneas. Un distrito informado inactivo tampoco tiene cobertura, aunque se envíe un código postal cubierto; se aplica [Gestión §3.1.1](./api-contract-gestion-despachos.md#311-administración-de-distritos--13112026), conservando este formato público.
- Con `lineas`, Despacho agrupa los SKU repetidos, consulta a Productos en lote y calcula el peso y volumen totales; el canal no envía esos totales.
- La cotización no crea un pedido ni un despacho.
- Decisión de redondeo confirmada el 05/11/2026: el cálculo utiliza aritmética decimal y peso/volumen exactos acumulados, sin redondeos por unidad o línea. `costoEnvio` se eleva directamente al siguiente múltiplo de `0.50 PEN` y se representa con dos decimales; un múltiplo exacto se conserva. Ejemplos: `8.12 → 8.50`, `8.78 → 9.00`, `15.13 → 15.50`, `8.5001 → 9.00`.
- `volumenTotalM3` se eleva a tres decimales únicamente al elaborar el resultado: `0.021112 → 0.022`, `0.025822 → 0.026`, `0.025135 → 0.026`. La fórmula tarifaria utiliza el volumen exacto, no ese valor presentado. El histórico conserva ambos valores y el ajuste monetario de redondeo; respuesta y columnas de resultado coinciden.
- Después de resolver cobertura y validar las líneas, se admiten como máximo 100 SKU únicos. Superar ese límite se rechaza antes de llamar a Productos; no se particiona la consulta en este alcance.
- Solo se emite una cotización exacta cuando todos los SKU existen, son comercialmente elegibles y tienen perfil físico válido. `INACTIVO` y `BORRADOR` impiden cotizar conforme a H-G2-02, cerrado el 02/11/2026; el estado efectivo de variantes se precisa en §5.2.3.
- Antes de devolver `200 OK` con `tipoCotizacion=EXACTA`, Gestión guarda en `cotizaciones` el resultado y la instantánea de la tarifa aplicada. Se registra en F-01, aunque todavía no exista un pedido: `pedido_id` y `despacho_id` quedan nulos. La respuesta pública mantiene su esquema y no agrega un identificador de cotización obligatorio.
- La consulta de solo cobertura, los destinos sin cobertura, las zonas sin tarifa y cualquier rechazo no generan una fila en `cotizaciones`. No se inventan importes para satisfacer las columnas obligatorias.
- Un fallo al confirmar la escritura responde `500 DESP_ERROR_PERSISTENCIA_COTIZACION`, sin costo ni registro parcial. No se interpreta como indisponibilidad de Productos.
- La instantánea histórica no es una caché del catálogo ni una reserva de tarifa; no sustituye la consulta física de F-02 ni habilita aceptación, cancelación o vencimiento de cotizaciones. El modelo y el detalle interno de persistencia se documentan en [modelo de datos §3.1](../arquitectura/modelo-datos.md#31-zonas-y-tarifas-gestión-de-despachos-escribe-f-01).

Respuesta con cobertura, sin productos (`200 OK`):

```json
{
  "coberturaDisponible": true,
  "nombreZona": "Lima Centro",
  "distrito": "San Borja",
  "codigoPostal": "15036"
}
```

Respuesta con cobertura, con productos (`200 OK`):

```json
{
  "coberturaDisponible": true,
  "tipoCotizacion": "EXACTA",
  "nombreZona": "Lima Centro",
  "direccion": "Av. Javier Prado Este 2465",
  "distrito": "San Borja",
  "codigoPostal": "15036",
  "pesoTotalKg": 2.65,
  "volumenTotalM3": 0.022,
  "costoEnvio": 10.00,
  "moneda": "PEN",
  "plazoEstimadoDiasHabiles": 2,
  "fechaEstimadaEntrega": "2026-09-25",
  "cotizadoEn": "2026-09-23T18:30:00Z"
}
```

Respuesta sin cobertura (`200 OK`), con o sin productos:

```json
{
  "coberturaDisponible": false,
  "nombreZona": null,
  "distrito": "San Borja",
  "codigoPostal": "15036",
  "mensaje": "La dirección se encuentra fuera de nuestra zona de cobertura"
}
```

El costo, el plazo y la `fechaEstimadaEntrega` solo se devuelven con `tipoCotizacion=EXACTA`, es decir, cuando el cuerpo incluye `lineas`. Si se omite `lineas`, la respuesta confirma únicamente la cobertura, la zona y los datos del destino; los canales validan la cobertura en pantalla y, si no hay cobertura, se muestra el mensaje correspondiente sin costo ni plazo. Despacho calcula tanto el plazo como la fecha estimada de entrega. Los canales pueden comunicar `fechaEstimadaEntrega` al cliente y Ventas puede conservarla como referencia, pero no debe enviarla al crear el despacho. Las promociones, incluido el envío gratuito, y el importe finalmente cobrado al cliente pertenecen a Ventas y Postventa y no modifican el costo logístico calculado por Despacho.

Errores propios:

| Código | HTTP | Condición |
|---|---|---|
| `DESP_ERROR_DESTINO_REQUERIDO` | `400` | No se informó `distrito` |
| `DESP_ERROR_LINEAS_VACIAS` | `400` | Se envió `lineas` sin elementos |
| `DESP_ERROR_CANTIDAD_INVALIDA` | `400` | Cantidad menor o igual a cero |
| `DESP_ERROR_LIMITE_SKUS_EXCEDIDO` | `400` | Más de 100 SKU únicos, después de agrupar las líneas |
| `DESP_ERROR_PRODUCTO_NO_ENCONTRADO` | `422` | Productos no reconoce un SKU |
| `DESP_ERROR_PRODUCTO_NO_ELEGIBLE` | `422` | En F-01, SKU existente con estado efectivo `INACTIVO` o `BORRADOR`; en F-01/F-02, SKU padre no físico conforme al acuerdo D-G2-02 de §5.2.4 |
| `DESP_ERROR_DATOS_FISICOS_INCOMPLETOS` | `422` | Productos comunica datos físicos incompletos o inválidos mediante el error de negocio acordado |
| `DESP_ERROR_RESPUESTA_PRODUCTOS_INVALIDA` | `502` | Respuesta malformada, incoherente o que incumple la integridad del lote |
| `DESP_ERROR_PRODUCTOS_NO_DISPONIBLE` | `503` | No fue posible consultar Productos |
| `DESP_ERROR_PERSISTENCIA_COTIZACION` | `500` | No se pudo confirmar la instantánea obligatoria de la cotización |
| `DESP_ERROR_LIMITE_COTIZACION` | `429` | El canal excedió su límite |


### 5.2. Integración de Despacho con Productos y Ofertas

Esta operación pertenece a Productos y Ofertas. Ambos equipos acordaron la ruta, las unidades y la autorización descritas a continuación.

- **Método:** `POST`
- **Ruta:** `/api/v1/productos/datos-fisicos/consulta`
- **Consumidor:** backend de Despacho, identificado por el `client_id` `modulo-despacho`.
- **Autenticación:** token de servicio con `aud=api-productos` y scope `productos:fisicos:leer`.
- **Unidades:** peso en kilogramos (`pesoKg`) y dimensiones en centímetros (`dimensionesCm`).

```json
{
  "skus": [
    "POL-NEG-M",
    "ZAP-RUN-42"
  ]
}
```

```json
{
  "productos": [
    {
      "sku": "POL-NEG-M",
      "estado": "ACTIVO",
      "pesoKg": 0.25,
      "dimensionesCm": {
        "largo": 30,
        "ancho": 25,
        "alto": 3
      },
      "actualizadoEn": "2026-09-22T14:00:00Z"
    },
    {
      "sku": "ZAP-RUN-42",
      "estado": "ACTIVO",
      "pesoKg": 1.4,
      "dimensionesCm": {
        "largo": 35,
        "ancho": 22,
        "alto": 13
      },
      "actualizadoEn": "2026-09-20T10:00:00Z"
    }
  ],
  "noEncontrados": []
}
```

Despacho calcula por cada línea:

```text
volumenUnitarioM3 = (largoCm / 100) × (anchoCm / 100) × (altoCm / 100)
pesoLineaKg = pesoKg × cantidad
volumenLineaM3 = volumenUnitarioM3 × cantidad
```

No se accede directamente a la base de datos de Productos. La consulta debe ser en lote para evitar una llamada por línea. Puede utilizarse una caché breve, pero los datos siguen siendo propiedad de Productos y Ofertas.

Despacho no reenvía a Productos el token recibido de Marketplace o Chatbot. Obtiene y utiliza su propio token de servicio, porque Productos autoriza a `modulo-despacho`, no al canal que inició la cotización.

#### 5.2.1. Preparación y validación del lote

- Se envían entre 1 y 100 SKU únicos por petición. Los SKU son identificadores opacos: no se cambian mayúsculas, ceros iniciales ni su identidad al agruparlos. G2 conserva la suma de cantidades por SKU y la usa en todos los cálculos.
- En una respuesta `200`, `productos` y `noEncontrados` son obligatorios. Cada SKU solicitado debe aparecer exactamente una vez en uno de esos arreglos. Se rechazan duplicados dentro de cada arreglo o entre ambos, omisiones y SKU inesperados.
- Cada producto debe contener `sku`, `estado` en `ACTIVO`, `INACTIVO` o `BORRADOR`, `pesoKg` positivo, `dimensionesCm` con tres magnitudes positivas y `actualizadoEn` válido en ISO 8601. Un estado desconocido, JSON ilegible o un campo obligatorio inválido hace inválida la respuesta.
- Primero se valida la integridad de toda la respuesta; después se interpretan los SKU inexistentes y las reglas comerciales. Un SKU correctamente clasificado en `noEncontrados` es un resultado de negocio, no una omisión del proveedor.
- Una respuesta inválida produce `502 DESP_ERROR_RESPUESTA_PRODUCTOS_INVALIDA`. G2 no completa datos, no calcula costos parciales y no persiste una cotización ni crea un despacho con una parte del lote.
- En el alcance inicial G2 rechaza más de 100 SKU únicos con el error de §5.1, tanto en F-01 como en F-02. Las consultas de solo cobertura y las respuestas sin cobertura de F-01 conservan su precedencia: no consultan Productos.

#### 5.2.2. Traducción de errores y estado comercial

Las reglas H-G2-01/02/03 quedaron homologadas el 02/11/2026 en OpenAPI G6 `0.6.0-rc.3`. Su aceptación, las precisiones D-G2-01/02 y las verificaciones de implementación se registran por separado en §11.1 [cite: 1, 3].

| Resultado de Productos | Tratamiento de G2 |
|---|---|
| `422 DATOS_FISICOS_INCOMPLETOS`, con `details.skusAfectados` contractual | `422 DESP_ERROR_DATOS_FISICOS_INCOMPLETOS`, preservando esos SKU en el error público |
| `200` íntegro con `noEncontrados` no vacío | `422 DESP_ERROR_PRODUCTO_NO_ENCONTRADO`, identificando esos SKU |
| `422 SKU_INVALIDO` por SKU padre con variantes, con identificación contractual de los afectados | `422 DESP_ERROR_PRODUCTO_NO_ELEGIBLE` en F-01/F-02, conservando `details.skusAfectados`, conforme al acuerdo de §5.2.4 y su revisión exigible |
| Timeout, fallo de conexión o error técnico `5xx` de Productos, incluida su indisponibilidad/PostgreSQL | `503 DESP_ERROR_PRODUCTOS_NO_DISPONIBLE` |
| `200` malformado o incoherente, o error que incumple el esquema exigible de su revisión acordada | `502 DESP_ERROR_RESPUESTA_PRODUCTOS_INVALIDA` |

Un `422` distinto no se traduce automáticamente como producto inexistente ni como datos incompletos: se contrasta su código con el contrato de G6. Un error no contemplado se trata como respuesta no conforme. Un `401`/`403` de G6 corresponde a la credencial saliente de G2, no a la del canal; G2 informa dependencia no disponible, registra el diagnóstico técnico y no reenvía ese rechazo como si el consumidor hubiera fallado su autenticación. No se reintenta un rechazo de permiso de forma indiscriminada.

La regla comercial homologada es:

| Estado del SKU existente y con perfil válido | F-01: cotización | F-02: pedido pagado y preparado |
|---|---|---|
| `ACTIVO` | Puede continuar el cálculo | Puede continuar la recepción |
| `INACTIVO` | `422 DESP_ERROR_PRODUCTO_NO_ELEGIBLE` | No se rechaza solo por este estado |
| `BORRADOR` | `422 DESP_ERROR_PRODUCTO_NO_ELEGIBLE` | No se rechaza solo por este estado |

Un SKU existente no pasa a `noEncontrados` por su estado comercial. La elegibilidad de venta de F-01 se aplica después de validar el perfil y el lote; F-02 conserva las demás validaciones de cobertura, identidad, existencia e integridad física. Si G6 comunica datos incompletos, se aplica ese error antes de cualquier cálculo.

#### 5.2.3. D-G2-01 — Estado efectivo de variantes

G2 aceptó el 03/11/2026 que `estado` sea la proyección efectiva del SKU en esta consulta y G6 ratificó ese mismo día el cierre D-G2-01. La implementación y regresiones del proveedor están fusionadas en el PR backend #162, merge `eeaf39ad666d7bc5a11c0b2a80d9c8c4b0016414`. La sincronización normativa y las regresiones del consumidor se siguen en §11.1 [cite: 5, 6, 7].

| Estado propio de variante en G6 | Estado del producto padre | `estado` de la consulta |
|---|---|---|
| `ACTIVA` | `ACTIVO` | `ACTIVO` |
| `ACTIVA` | `INACTIVO` | `INACTIVO` |
| `ACTIVA` | `BORRADOR` | `BORRADOR` |
| `INACTIVA` | Cualquiera de los tres estados | `INACTIVO` |
| `BORRADOR` | Cualquiera de los tres estados | `BORRADOR` |

Un producto simple conserva su estado comercial en la consulta. G6 calcula esta proyección sin cambiar estados persistidos, versiones ni reglas de activación. G2 utiliza el estado recibido; no solicita ni reconstruye la jerarquía padre/variante. F-01 aplica la elegibilidad de §5.2.2; F-02 no rechaza automáticamente un pedido declarado pagado, preparado y listo para entrega por `INACTIVO` o `BORRADOR`. Un estado admitido no garantiza perfil físico completo.

#### 5.2.4. D-G2-02 — SKU padre e identificación del rechazo

**Estado:** acuerdo semántico bilateral cerrado el 04/11/2026, tras la aceptación de G6 del 03/11 y la confirmación final de G2 del 04/11. Campo, afectados, orden, traducción y precedencia están acordados; queda pendiente sincronizarlos con la revisión normativa de G6 [cite: 7, 8, 9]. La línea base `0.6.0-rc.3` exige `details.skusAfectados` para `DATOS_FISICOS_INCOMPLETOS`, no para todo `SKU_INVALIDO`.

El `sku_base` de un producto con variantes existe, pero no representa una unidad física vendible. G2 acepta que su consulta provoque `422 SKU_INVALIDO` y rechazo íntegro: no clasificarlo como inexistente en `noEncontrados`, inventarle medidas ni devolver perfiles parciales. El SKU base de un producto simple sin variantes sigue siendo consultable si tiene un perfil físico válido.

Para esta condición, G6 debe incluir `details.skusAfectados` obligatorio, no vacío y sin duplicados, con las restricciones de identificación de §3.5. Contiene todos y únicamente los SKU padre del lote que cumplen esa condición, preservados literalmente y en orden de primera aparición en la solicitud. Esta obligación no se extiende a las otras causas de `SKU_INVALIDO` sin acuerdo.

Fragmento acordado del problema de G6, complementado con los demás campos de su Problem contractual:

```json
{
  "status": 422,
  "code": "SKU_INVALIDO",
  "details": {
    "skusAfectados": ["ZAP-BASE", "POL-BASE"]
  }
}
```

G2 responde en F-01 y F-02 con `422 DESP_ERROR_PRODUCTO_NO_ELEGIBLE`, preservando la lista validada en el mismo campo. En F-02 este rechazo se debe a un identificador no válido como unidad física, no al estado comercial. Se rechaza el lote completo sin costo, nueva cotización ni despacho parcial.

La traducción se aplica al caso expresamente acordado, contrastando la revisión exigible del proveedor. El acuerdo y el merge están enlazados en §11.1; la publicación del OpenAPI que los incorpora sigue pendiente y no se atribuye esta ampliación a la línea base rc.3 anterior. No se interpreta cualquier `422 SKU_INVALIDO` como SKU padre: G6 también utiliza ese código para otras validaciones. No se extraen identificadores del texto libre de `detail` ni se inventan a partir del lote completo. Bajo la revisión que exige esta ampliación, un detalle ausente, vacío, con tipos incorrectos, duplicados o SKU ajenos produce `502 DESP_ERROR_RESPUESTA_PRODUCTOS_INVALIDA`.

La precedencia de G6 para lotes mixtos, aceptada por G2 el 04/11/2026, es [cite: 7, 8]:

1. Solicitud estructuralmente inválida: aplicar las validaciones existentes.
2. Fallo técnico: `503 SERVICIO_NO_DISPONIBLE`.
3. Al menos un SKU padre no físico: `422 SKU_INVALIDO`, con todos los afectados de esa clase.
4. En ausencia de esas bases, perfiles físicos incompletos: `422 DATOS_FISICOS_INCOMPLETOS`, con los afectados correspondientes.
5. Sin esos errores: `200` con clasificación íntegra entre `productos` y `noEncontrados`.

El código y el conjunto de afectados de la condición seleccionada no dependen del orden de los SKU; la lista conserva el orden de cada solicitud. Informar esos afectados no certifica que las demás líneas sean válidas. El `200` puede incluir SKU inexistentes: G2 valida primero la integridad y aplica la traducción de `noEncontrados` de §5.2.2 antes de la elegibilidad comercial. Esta precedencia rige la consulta física del proveedor; las validaciones previas de G2, incluida cobertura e idempotencia, conservan su recorrido. No se calculan ni persisten resultados parciales.

## 6. RT-04: seguimiento para Marketplace y Chatbot

### 6.1. Consultar por identificador de pedido

- **Método:** `GET`
- **Ruta:** `/api/v1/seguimientos/pedidos/{idPedido}`
- **Autenticación:** token de servicio con `tipo=servicio` y scope `seguimientos:leer`.
- **Consumidores:** Marketplace, Chatbot y Ventas y Postventa.

`idPedido` conserva literalmente su identidad: no se convierte a UUID, no se cambian mayúsculas ni se eliminan ceros iniciales. Debe contener entre 1 y 128 caracteres, sin espacios al inicio o al final. Un identificador inválido produce `400 DESP_ERROR_SOLICITUD_INVALIDA`; un pedido sin despacho real disponible produce `404 DESP_ERROR_DESPACHO_NO_ENCONTRADO`. Los despachos simulados no se exponen por esta consulta. La autenticación y autorización se aplican antes de consultar la existencia del pedido, conforme a §3.5.

La respuesta `200` incluye `Cache-Control: no-store`. Estado, fecha programada e hitos se leen de una misma instantánea de Gestión, sin consultar otros módulos. Los hitos contienen únicamente transiciones del historial, incluida la creación, ordenadas por `registrado_en` ascendente y por identificador de entrada en caso de empate; las reasignaciones, los reordenamientos y la recepción en el centro no crean hitos públicos adicionales. `actualizadoEn` corresponde a la última actualización del despacho.

```json
{
  "idPedido": "PED-2026-00891",
  "estado": "EN_CAMINO",
  "estadoEtiqueta": "En camino",
  "fechaProgramada": "2026-09-25",
  "distrito": "San Borja",
  "hitos": [
    {
      "estado": "PENDIENTE_ASIGNACION",
      "etiqueta": "En centro de despacho",
      "fecha": "2026-09-23T10:00:00Z"
    },
    {
      "estado": "ASIGNADO",
      "etiqueta": "Asignado a repartidor",
      "fecha": "2026-09-23T14:30:00Z"
    },
    {
      "estado": "EN_CAMINO",
      "etiqueta": "En camino",
      "fecha": "2026-09-23T16:00:00Z"
    }
  ],
  "actualizadoEn": "2026-09-23T16:00:00Z"
}
```

El campo `estado` utiliza el siguiente catálogo. `FALLIDO` representa un intento no completado y no debe interpretarse como el cierre definitivo del despacho.

| Estado | Significado para el consumidor | Final |
|---|---|---|
| `PENDIENTE_ASIGNACION` | El despacho fue creado y espera la asignación de un repartidor. | No |
| `ASIGNADO` | El despacho ya tiene repartidor, pero todavía no ha salido del centro. | No |
| `EN_CAMINO` | El repartidor recogió el paquete y comenzó el traslado al destinatario. | No |
| `FALLIDO` | La entrega no se completó o terminó la jornada sin realizarla; todavía puede reprogramarse. | No |
| `ENTREGADO` | La entrega fue confirmada correctamente. | Sí |
| `DEVUELTO_A_ORIGEN` | El paquete regresó al centro y la operación terminó sin entrega; no habrá otro intento. | Sí |
| `CANCELADO` | Ventas anuló el pedido antes de completar el despacho. | Sí |

Los estados finales son `ENTREGADO`, `DEVUELTO_A_ORIGEN` y `CANCELADO`; no admiten nuevas transiciones. Para consultar las transiciones, precondiciones y efectos completos, véase el [diagrama de estados del despacho](../arquitectura/diagrama-estados-despacho.md).

`estadoEtiqueta` y las etiquetas de los hitos proporcionan una descripción pública sin detalles operativos sensibles. Cada canal puede adaptar su redacción para la interfaz.

La respuesta nunca incluye:

- Coordenadas.
- Dirección completa o referencia.
- Teléfono, correo o nombre del destinatario.
- Identidad, teléfono o ubicación del repartidor.
- Motivo o comentario interno de un fallo.
- Evidencias fotográficas.

Un fallo reprogramado muestra hitos con etiquetas públicas como `No entregado, regresando al centro` y `Entrega reprogramada`, sin exponer el motivo operativo.

Cuando la transición de `FALLIDO` a `PENDIENTE_ASIGNACION` conserva `nuevaFechaProgramada`, su etiqueta es `Entrega reprogramada para [fecha]`, con fecha ISO `YYYY-MM-DD` tomada de esa entrada histórica. No se reemplaza por la fecha programada actual del despacho. Para entradas históricas sin ese dato se conserva la etiqueta `Entrega reprogramada`.

## 7. F-02: integración con Ventas y Postventa

### 7.1. Crear un despacho para un pedido listo para entrega

- **Método:** `POST`
- **Ruta:** `/api/v1/despachos`
- **Autenticación:** token de servicio con `tipo=servicio`, `sub=modulo-ventas`, `aud=api-despacho` y scope `despachos:crear`.
- **Idempotencia:** `Idempotency-Key` es obligatorio, debe ser un UUID v4 de hasta 128 caracteres y se combina con la unicidad de negocio por `idPedido`. Un reintento con la misma clave devuelve el resultado original sin crear otro despacho.

```json
{
  "idPedido": "PED-2026-00891",
  "destinatario": {
    "nombre": "Carlos Mendoza",
    "telefono": "+51987654321"
  },
  "destino": {
    "direccion": "Av. Javier Prado Este 2465",
    "referencia": "Frente al parque",
    "departamento": "Lima",
    "provincia": "Lima",
    "distrito": "San Borja",
    "codigoPostal": "15036",
    "coordenadas": {
      "latitud": -12.08945,
      "longitud": -77.00342
    }
  },
  "lineas": [
    {
      "sku": "POL-NEG-M",
      "cantidad": 5
    },
    {
      "sku": "ZAP-RUN-42",
      "cantidad": 1
    }
  ]
}
```

`lineas` es el arreglo de todos los productos incluidos en el pedido. Cada elemento identifica un producto por `sku` y expresa cuántas unidades contiene mediante `cantidad`. Las líneas no representan paquetes físicos: por ejemplo, cinco polos dentro de una misma bolsa se informan como una línea con cantidad `5` y el despacho continúa consumiendo un solo paquete lógico.

Las coordenadas y `codigoPostal` son opcionales. Debe enviarse al menos una línea, cada `sku` es obligatorio y cada `cantidad` debe ser mayor que cero.

La llamada autenticada con `sub=modulo-ventas` declara que el pedido está pagado, preparado y listo para entrega. Despacho no consulta el estado del pedido en Ventas: valida su propia cobertura y los datos recibidos y, si son correctos, crea inmediatamente el despacho en `PENDIENTE_ASIGNACION`. La creación no asigna todavía un repartidor ni incorpora el pedido a una ruta.

Antes de persistir el despacho, Despacho consulta en lote a Productos los datos físicos de las líneas y calcula `pesoTotalKg` y `volumenTotalM3` conforme a la sección 5.2. Esos valores se guardan para la asignación y el control de capacidad de la furgoneta. `cantidadPaquetes` no se recibe de Ventas: se registra internamente con valor `1`. Esta consulta obtiene únicamente datos físicos; Despacho no consulta, reserva ni modifica stock.

F-02 no exige una cotización previa ni recibe un identificador de cotización. No usa una instantánea histórica de `cotizaciones` como fuente física y no escribe en esa tabla al recibir el pedido. Para un pedido ya registrado, la respuesta idempotente se resuelve antes de repetir la consulta a Productos. Para uno nuevo se aplican el límite, la integridad del lote y la regla comercial homologada en §5.2: `INACTIVO` o `BORRADOR` no constituyen por sí solos un motivo de rechazo. D-G2-01 precisa el estado efectivo recibido; D-G2-02, cerrado semánticamente el 04/11/2026, exige el rechazo por SKU padre no físico con los afectados identificados y la precedencia de §5.2.4. Su sincronización normativa se sigue en §11.1.

Despacho calcula `fechaProgramada` al crear el despacho a partir de la zona, el plazo aplicable y el calendario operativo.

Respuesta de creación, `201 Created`:

```json
{
  "idDespacho": "7f3c2a9e-4b1d-4c8e-9a2f-1d6b5e8c0a41",
  "idPedido": "PED-2026-00891",
  "codigoRastreoInterno": "TRK-000234",
  "estado": "PENDIENTE_ASIGNACION",
  "idZona": "3899d20d-d800-4a8f-9970-713e518c28d0",
  "fechaProgramada": "2026-09-25",
  "creadoEn": "2026-09-23T18:45:00Z",
  "codigoOperativo": "DSP-000234",
  "esSimulacion": false,
  "pesoTotalKg": 0.37,
  "volumenTotalM3": 0.004416,
  "cantidadPaquetes": 1
}
```

`idDespacho` e `idZona` son UUID y se tratan como cadenas opacas; `codigoOperativo` y `codigoRastreoInterno` son legibles para personas. `fechaProgramada` es una fecha civil `YYYY-MM-DD` del calendario `America/Lima`, calculada en días hábiles de lunes a viernes; `creadoEn` es un instante ISO 8601 en UTC.

Reglas del cuerpo:

- Son obligatorios `idPedido`, `destinatario.nombre`, `destinatario.telefono`, `destino.direccion`, `destino.distrito` y al menos una línea. Los opcionales pueden omitirse o enviarse como `null`.
- Los textos deben ser cadenas JSON; se recortan los espacios de los extremos. `telefono` no tiene formato validado; se recomienda E.164, por ejemplo `+51987654321`.
- Se rechaza con `400 DESP_ERROR_CAMPOS_INVALIDOS` cualquier campo no declarado.
- `distrito` debe coincidir con un distrito activo de una zona con tarifa vigente, con tildes incluidas.
- El cuerpo no puede superar 1 MiB (`413 DESP_ERROR_SOLICITUD_DEMASIADO_GRANDE`).

Si el pedido ya tiene despacho, se responde `200 OK` con el recurso existente y no se crea otro. Hay un único despacho por `idPedido` y su `idDespacho` no cambia en los reintentos de entrega.

La clave de idempotencia solo se registra cuando el despacho queda creado (`201` o `200`). Un intento rechazado (`400`, `422` o `503`) no la consume, de modo que el pedido corregido puede reintentarse con la misma clave. Una clave ya registrada con un cuerpo distinto responde `409 DESP_ERROR_OPERACION_REUTILIZADA`. Si eso ocurre tras un `503` o un tiempo agotado, el despacho pudo haberse creado; repetir el cuerpo original devuelve el resultado registrado. Si el destino no tiene cobertura, se responde `422` con `DESP_ERROR_SIN_COBERTURA`. Los SKU inexistentes o sin datos físicos responden con los errores definidos en la sección 5.1; si Productos no está disponible, se responde `503` y no se crea el despacho.

### 7.2. Cancelar por anulación del pedido

- **Método:** `POST`
- **Ruta:** `/api/v1/despachos/pedidos/{idPedido}/cancelacion`
- **Autenticación:** token de servicio con `tipo=servicio`, `sub=modulo-ventas`, `aud=api-despacho` y scope `despachos:cancelar`.
- **Idempotencia:** `Idempotency-Key` es obligatorio y debe ser un UUID v4 de hasta 128 caracteres. Una repetición con la misma clave conserva el resultado original y no duplica la transición, la auditoría ni el evento.

```json
{
  "motivo": "PEDIDO_ANULADO",
  "observacion": "Anulación confirmada por Ventas"
}
```

Comportamiento:

| Estado actual | Resultado |
|---|---|
| `PENDIENTE_ASIGNACION` | `200 OK`; cambia a `CANCELADO` y emite `DESPACHO_CANCELADO` |
| `ASIGNADO` | `200 OK`; cambia a `CANCELADO`, libera capacidad y emite `DESPACHO_CANCELADO` |
| `EN_CAMINO` | `409 DESP_ERROR_ESTADO_INCOMPATIBLE` con `estadoActual`; no se cancela |
| `FALLIDO` | `202 Accepted` con `estado=FALLIDO`; registra la anulación, no emite `DESPACHO_CANCELADO` y obliga al cierre en F-04, que emite `DESPACHO_DEVUELTO_A_ORIGEN` cuando el paquete vuelve al centro |
| `ENTREGADO`, `DEVUELTO_A_ORIGEN` o `CANCELADO` | `409 DESP_ERROR_ESTADO_INCOMPATIBLE` con `estadoActual`. La repetición con la misma clave de una cancelación aceptada devuelve su resultado original |

`motivo` es obligatorio, texto de hasta 128 caracteres; Ventas envía `PEDIDO_ANULADO`. `observacion` es opcional, de hasta 500 caracteres. Como la clave identifica el cuerpo, un reintento debe repetir el mismo `motivo` y la misma `observacion`.

Otras respuestas: `404 DESP_ERROR_DESPACHO_NO_ENCONTRADO` si no hay despacho para el pedido; `409 DESP_ERROR_OPERACION_REUTILIZADA` si la clave ya identifica otra cancelación. `409 DESP_ERROR_ASIGNACION_EN_PROCESO`, `409 DESP_ERROR_CANCELACION_EN_PROCESO`, `409 DESP_ERROR_DESPACHO_DESACTUALIZADO` y `503 DESP_ERROR_CANCELACION_NO_DISPONIBLE` pueden reintentarse con la misma clave.

Registrar la anulación en `FALLIDO` incrementa la versión del despacho y compite con reprogramación/cierre, aunque conserve el estado. Si otra operación ganó al confirmar, responde `409` sin registrar parcialmente la marca; una nueva ejecución debe evaluar el estado canónico vigente. Si ganó una reprogramación, la cancelación posterior se evalúa sobre `PENDIENTE_ASIGNACION`; no se descarta por conservar una lectura anterior de `FALLIDO`. Una repetición ya aceptada sigue las reglas de idempotencia de §9.

```json
{
  "idPedido": "PED-2026-00891",
  "idDespacho": "7f3c2a9e-4b1d-4c8e-9a2f-1d6b5e8c0a41",
  "estado": "CANCELADO",
  "procesadoEn": "2026-09-23T19:00:00Z"
}
```

### 7.3. Eventos salientes hacia Ventas y Postventa

Despacho registra el evento en una bandeja de salida dentro de la misma transacción del cambio. La entrega inicial se realizará por webhook HTTPS. La URL pertenece a Ventas y debe configurarse mediante una variable segura.

- **Método:** `POST`
- **Ruta confirmada por Ventas:** `/api/v1/integraciones/despachos/eventos`
- **Autenticación:** token de servicio emitido por Seguridad con `tipo=servicio`, `sub=modulo-despacho`, `aud=api-ventas` y scope `ventas:pedidos:estado`.
- **Idempotencia:** Ventas deduplica por `idEvento`.

```json
{
  "idEvento": "8e191bab-6ec4-4fa4-b70e-d90e83bd48df",
  "tipo": "DESPACHO_EN_CAMINO",
  "idPedido": "PED-2026-00891",
  "idDespacho": "7f3c2a9e-4b1d-4c8e-9a2f-1d6b5e8c0a41",
  "estadoAnterior": "ASIGNADO",
  "estadoNuevo": "EN_CAMINO",
  "ocurridoEn": "2026-09-23T19:20:00Z",
  "datos": {}
}
```

| Estado nuevo | Tipo de evento | Datos adicionales | Efecto acordado en Ventas |
|---|---|---|---|
| `ASIGNADO` | `DESPACHO_ASIGNADO` | `fechaProgramada` | Conserva `EN_PREPARACION`; registra el hito. |
| `EN_CAMINO` | `DESPACHO_EN_CAMINO` | Ninguno | Cambia `EN_PREPARACION` a `DESPACHADO`. |
| `ENTREGADO` | `DESPACHO_ENTREGADO` | `fechaEntrega`; `recibidoPor` opcional, texto libre de hasta 120 caracteres | Cambia `DESPACHADO` a `ENTREGADO`. |
| `FALLIDO` | `DESPACHO_FALLIDO` | `motivo` (código del catálogo de motivos de fallo), `numeroIntento` | Conserva `DESPACHADO`; registra el intento para auditoría. |
| `PENDIENTE_ASIGNACION` reprogramado | `DESPACHO_REPROGRAMADO` | `nuevaFechaProgramada` | Conserva el estado comercial y actualiza la fecha estimada de entrega. |
| `DEVUELTO_A_ORIGEN` | `DESPACHO_DEVUELTO_A_ORIGEN` | `motivoCierre`, texto libre del gestor de hasta 500 caracteres | Cambia `DESPACHADO` a `ANULADO` y habilita el tratamiento comercial correspondiente. |
| `CANCELADO` | `DESPACHO_CANCELADO` | `motivo` recibido en la cancelación | Confirma idempotentemente la cancelación solicitada; no reabre ni vuelve a modificar un pedido ya anulado. |

Los siete tipos anteriores son eventos logísticos aceptados por Ventas, no siete estados adicionales de su pedido. Ventas conserva su propia máquina de estados comercial y aplica el efecto indicado para cada evento.

La política de intentos pertenece a Despacho: el máximo inicial es `2`, puede configurarse dentro del módulo y no se recibe desde Ventas. `DESPACHO_FALLIDO` informa un intento no exitoso, pero no representa necesariamente un cierre definitivo. Solo `DESPACHO_DEVUELTO_A_ORIGEN` comunica que la operación logística terminó sin entrega y permite que Ventas y Postventa decida la anulación, devolución comercial o reembolso que corresponda.

Despacho no envía una `guiaRemision`, no solicita reembolsos y no informa importes a devolver. Esas responsabilidades permanecen fuera de este contrato.

Formato del sobre: `idEvento` e `idDespacho` son UUID; `ocurridoEn` y `fechaEntrega` son instantes ISO 8601 en UTC con `Z` y precisión fraccionaria variable; `fechaProgramada` y `nuevaFechaProgramada` son fechas `YYYY-MM-DD`. La petición lleva `Authorization: Bearer <token>` y `Content-Type: application/json`; no incluye `X-Correlation-Id`, por lo que la correlación se hace con `idEvento` e `idDespacho`.

`motivo` de `DESPACHO_FALLIDO` usa los códigos del catálogo de Gestión: `CLIENTE_AUSENTE`, `DATOS_DE_CONTACTO_ERRONEOS`, `DIRECCION_NO_UBICADA`, `PAQUETE_DANADO`, `RECHAZO_DEL_PAQUETE`, `ZONA_INACCESIBLE` y `NO_INTENTADO`. El catálogo puede ampliarse, así que Ventas no rechaza un código desconocido. `NO_INTENTADO` lo registra el cierre de jornada para los pendientes no visitados y, según el catálogo, no consume intento.

Orden posible: en cada intento, `ASIGNADO` se genera antes que `EN_CAMINO`; tras `FALLIDO` pueden llegar `REPROGRAMADO`, otro `ASIGNADO` y otro `EN_CAMINO`, o bien `DEVUELTO_A_ORIGEN`. Los reintentos de entrega pueden alterar el orden de llegada, así que Ventas no retrocede su estado y responde `2xx` a eventos atrasados o duplicados. Reasignar el repartidor de un despacho ya asignado no genera evento.

Si Ventas no responde con `2xx`, Despacho realiza hasta cinco reintentos con espera creciente (unos 30 s, 1, 2, 4 y 8 minutos). Cualquier otra respuesta, incluidos `4xx`, o un tiempo agotado cuenta como fallo; el plazo predeterminado es 3 s de conexión y 5 s de lectura. El mismo `idEvento` se conserva. Tras agotarlos, el evento queda como `ENVIO_FALLIDO`; un gestor puede devolverlo a la cola con `POST /api/v1/despachos/{idDespacho}/eventos-ventas/{idEvento}/reenvio`, que conserva el contenido y el `idEvento`. Los despachos simulados no generan eventos externos.

## 8. Integración con Seguridad y Usuarios

### 8.1. Validación local de tokens

Despacho valida los JWT localmente, sin consultar a Seguridad en cada solicitud:

1. Obtiene la configuración desde `GET /api/v1/auth/.well-known/openid-configuration`.
2. Obtiene las claves públicas desde `GET /api/v1/auth/.well-known/jwks.json` y las mantiene en caché.
3. Verifica firma, algoritmo permitido, vencimiento y emisor.
4. Para una persona gestora, exige `tipo=acceso` y el rol `GESTOR_DESPACHO`.
5. Para un repartidor, exige `tipo=acceso` y resuelve mediante `sub` un perfil local activo vinculado al usuario.
6. Para un módulo, exige `tipo=servicio`, que `aud` contenga `api-despacho` y el scope propio de la operación.

No se exige el rol ficticio `SERVICIO_INTEGRACION` ni un claim separado `clientId`: la identidad técnica ya está en `sub`. Tampoco se combina el token con una API key.

Ejemplo conceptual de token de usuario:

```json
{
  "sub": "2f31c1b1-7568-41b8-bf91-342db68431df",
  "tipo": "acceso",
  "roles": ["GESTOR_DESPACHO"],
  "iss": "https://seguridad.example.com/api/v1/auth",
  "iat": 1789273200,
  "exp": 1789276800,
  "jti": "7f22750a-8365-48da-93cf-6e063159ec60"
}
```

Ejemplo conceptual de token de servicio:

```json
{
  "sub": "modulo-chatbot",
  "tipo": "servicio",
  "scope": "cotizaciones:calcular seguimientos:leer",
  "aud": ["api-despacho"],
  "iss": "https://seguridad.example.com/api/v1/auth",
  "iat": 1789273200,
  "exp": 1789276800,
  "jti": "ad6b5dda-c481-4ad3-907a-04a56d392de7"
}
```

Los valores de `iss` mostrados son ilustrativos y no deben copiarse en la configuración. Cada despliegue obtiene el emisor real mediante el documento de descubrimiento de su propio entorno. Seguridad confirmó `scope` como texto separado por espacios dentro del JWT de servicio y `aud` como la lista de APIs dueñas de los scopes concedidos.

### 8.2. Obtención de un token de servicio

Antes de llamar a otro módulo, cada backend obtiene su propio token de Seguridad. Las credenciales de cliente se conservan exclusivamente en el servidor y nunca se exponen al navegador, aplicación móvil ni módulo receptor.

- **Método:** `POST`
- **Ruta de Seguridad:** `/api/v1/auth/token`
- **Content-Type:** `application/x-www-form-urlencoded`

```text
grant_type=client_credentials
client_id=modulo-chatbot
client_secret=<secreto-del-chatbot>
scope=cotizaciones:calcular seguimientos:leer
```

Respuesta esperada:

```json
{
  "accessToken": "<jwt-de-servicio>",
  "tokenType": "Bearer",
  "expiresIn": 3600,
  "scopes": [
    "cotizaciones:calcular",
    "seguimientos:leer"
  ]
}
```

El arreglo `scopes` pertenece a la respuesta HTTP de emisión. Dentro del JWT emitido, el claim `scope` contiene esos valores separados por espacios. El canal utiliza después `Authorization: Bearer <jwt-de-servicio>` al llamar a Despacho. Del mismo modo, `modulo-despacho` solicita otro token con sus propias credenciales y el scope aceptado por Productos; nunca reutiliza el token del canal.

`modulo-despacho` no usa scopes internos entre sus propios microservicios: Gestión y Operación autorizan las rutas `/internal` por la identidad del servicio (`tipo=servicio`, `sub=modulo-despacho`, audiencia `api-despacho`, y firma, emisor y vencimiento válidos). Los permisos externos descritos en este contrato se mantienen.

### 8.3. Introspección de un usuario antes de una operación sensible

Seguridad ofrece `POST /api/v1/auth/introspeccion` para que un backend compruebe un token humano antes de una operación sensible. Esa ruta recibe el token humano en el cuerpo y se protege con el token de servicio del módulo que realiza la comprobación y el scope `tokens:introspeccion`.

Por ejemplo, si una persona administradora autoriza anular un pedido, Ventas valida o introspecciona el token de esa persona y luego llama a Despacho con el token técnico de `modulo-ventas` y `despachos:cancelar`. Despacho valida ese token de servicio localmente; no necesita introspeccionar el token técnico ni recibir el token humano de Ventas.

Seguridad todavía no ha concedido `tokens:introspeccion` a `modulo-despacho`; no se incluye como dependencia hasta que exista un caso de uso propio que lo requiera.

### 8.4. Creación y vinculación de un usuario repartidor

Seguridad confirmó que `REPARTIDOR` no será un rol global, que no incluirá un claim `idRepartidor` y que no expondrá un endpoint para crear cuentas de repartidores mediante el token de servicio de Despacho. Una persona con rol `ADMIN_SISTEMA` crea cada cuenta en Seguridad y Despacho mantiene el perfil operativo en su propia base de datos.

El flujo de vinculación esperado, pendiente de confirmación operativa por Seguridad, es el siguiente:

1. F-05 registra al repartidor local con `vinculacion=PENDIENTE`.
2. Un `ADMIN_SISTEMA` crea su cuenta en Seguridad mediante el procedimiento administrativo que ese módulo defina.
3. Despacho recibe el `usuario_id` generado por Seguridad y lo asocia al repartidor local.
4. Cuando llega un token humano, el backend utiliza su claim `sub` para localizar al repartidor vinculado.
5. El acceso a rutas y operaciones de campo depende del estado local del repartidor y de la propiedad del recurso.

Despacho almacena solamente el `usuario_id`, el vínculo y los estados operativos; nunca almacena ni transporta la contraseña definitiva. La entrega o activación de las credenciales pertenece a Seguridad. No se abre una jornada mientras el repartidor no esté vinculado y habilitado.

La baja lógica local impide nuevas jornadas, asignaciones y operaciones, pero no desactiva por sí sola la cuenta global. La desactivación o el bloqueo global corresponde a Seguridad. Todavía deben confirmar el canal administrativo para solicitar el alta o la baja global, cómo se comunicará el `usuario_id`, cómo se entregarán las credenciales iniciales y qué valor llevará `roles` en el token de una cuenta vinculada como repartidor.

### 8.5. Eventos de usuarios publicados por Seguridad

Despacho prevé consumir los eventos `usuario.desactivado`, `usuario.bloqueado`, `usuario.reactivado`, `usuario.desbloqueado` y `usuario.roles_cambiados` para mantener actualizado el acceso de los usuarios vinculados. Son notificaciones de cambios ya realizados por Seguridad, no comandos de Despacho ni scopes OAuth.

- `usuario.desactivado` y `usuario.bloqueado` impiden iniciar jornadas, recibir nuevas asignaciones o ejecutar operaciones.
- `usuario.reactivado` y `usuario.desbloqueado` actualizan el estado global, pero no revierten automáticamente una baja local.
- `usuario.roles_cambiados` actualiza oportunamente el acceso administrativo de quienes ejercen como `GESTOR_DESPACHO`.

Seguridad ha previsto publicar estos cambios mediante RabbitMQ, pero el contrato asíncrono todavía debe definir disponibilidad, exchange, routing keys, payloads, credenciales, permisos de consumo y política de reentrega. Hasta que ese contrato esté disponible, Despacho no asume que los eventos puedan consumirse en producción.

### 8.6. Acceso humano de ambas aplicaciones

Base documental: kit de integración de G7 §7, SPEC-05, SPEC-06, SPEC-09 y `specs/contrato-api/openapi.yaml` v1.1.0, copia facilitada el 05/11/2026. El OpenAPI indica `revision-local-no-publicada`; esta revisión no acredita publicación ni disponibilidad del servidor. El adaptador humano de G2 se prepara contra esa forma; la integración real y la aceptación siguen pendientes.

Todas las rutas siguientes parten de `/api/v1/auth`, en el origen de Seguridad. Los cuerpos de `POST` son JSON. El Gateway publica exclusivamente estas operaciones humanas, sin exponer por ello la emisión de tokens de servicio ni el buzón de pruebas.

| Operación | Entrada | Respuesta |
|---|---|---|
| `POST /login` | `correo`, `contrasena` | `200` con sesión o `{mfaRequerido:true, challengeToken, canal:"EMAIL", expiraEn:300}`. El desafío no concede acceso. |
| `POST /otp/solicitar` | `challengeToken`, `canal` (`EMAIL` en G2) | `202` con `canal`, `destinoEnmascarado`, `expiraEn` y `desafioExpiraEn`. No contiene el código. |
| `POST /otp/verificar` | `challengeToken`, `codigo` (seis dígitos) | `200` con sesión. El desafío se consume. |
| `POST /refresh` | `refreshToken` | `200` con nuevo par de tokens de la misma sesión; el refresco anterior deja de servir. |
| `POST /logout` | `refreshToken` | `204`, también si ya no existe o está revocado. No exige JWT de acceso. |
| `GET /me` | `Authorization: Bearer <accessToken>` | `200` con perfil del titular, o `401`. |

La sesión contiene `accessToken`, `refreshToken`, `tokenType:"Bearer"`, `expiresIn:900` y `usuario:{id,nombreCompleto,correo,roles}`. El acceso del gestor exige `GESTOR_DESPACHO` y MFA efectivo. El repartidor no necesita rol global: Operación resuelve su perfil activo por `sub`, según §8.1/§8.4. La comprobación visual del rol no sustituye la autorización del backend.

El código vence a los cinco minutos, admite tres intentos y tiene un límite de tres solicitudes por usuario en quince minutos. Reenviar invalida el código anterior del mismo desafío y no extiende su vencimiento. La pantalla utiliza el menor vencimiento entre código y desafío. Errores relevantes: `CREDENCIALES_INVALIDAS` (`401`), `CODIGO_INVALIDO` con `intentosRestantes` (`401`), `OTP_INTENTOS_AGOTADOS` (`401`), `OTP_EXPIRADO` (`410`), `TOKEN_INVALIDO` (`401`), `PASSWORD_CADUCADA` (`403`) y `DEMASIADAS_SOLICITUDES` (`429`, cabecera `Retry-After`). La UI selecciona mensajes por `code`, sin presentar detalles arbitrarios.

Acceso, refresco, usuario y desafío se mantienen en memoria; recargar exige login. El refresco es rotativo, con una sola renovación simultánea por sesión. Reutilizar uno usado revoca su familia sin afectar sesiones independientes. Logout limpia la memoria incluso si falla la conexión. Un JWT ya emitido puede seguir aceptándose hasta vencer en APIs que validan localmente; el cierre no acredita revocación instantánea en todos los servicios.

El doble `seguridad-local` reproduce este intercambio para Compose. Mantiene estado efímero y un buzón de códigos aleatorios accesible mediante `POST /pruebas/codigo-otp` con `challengeToken`, exclusivamente para desarrollo. El proxy Vite lo muestra en ambas apps solo en modo local; no es una operación de G7 ni una ruta pública del Gateway. Las herramientas antiguas pueden seguir usando el formulario `usuario`/`clave` del emisor local; el gestor recibe igualmente un desafío, nunca tokens antes de completarlo. El emisor no crea perfiles F-05 ni modifica las bases de negocio.

Pendientes externos: confirmar revisión contractual desplegada, ambiente y CORS; habilitar cuentas/activación/entrega de OTP y proporcionar los UUID de los repartidores. `VITE_SEGURIDAD_URL` cambia el origen del adaptador compartido `packages/cliente-api/src/sesion.ts`; si se mantiene el proxy Gateway, se configura `SEGURIDAD_URL` del backend. Las credenciales técnicas entre servicios se resuelven por separado.

## 9. Reglas de idempotencia y concurrencia

- La idempotencia de negocio es obligatoria en todos los comandos que modifican estado.
- Crear despacho es único por `idPedido`; reenviar el mismo pedido devuelve el despacho existente.
- Cancelar, asignar, reasignar, iniciar traslado, entregar, fallar, recibir, reprogramar y cerrar deben reconocer una repetición segura mediante la clave de negocio, la versión o una clave de operación.
- `Idempotency-Key` es obligatorio como UUID v4 de hasta 128 caracteres para `POST /api/v1/despachos` y `POST /api/v1/despachos/pedidos/{idPedido}/cancelacion`. En los demás comandos `POST` se exige únicamente cuando la operación lo declara expresamente.
- Una repetición exacta devuelve el resultado original sin duplicar historial, intento, reserva ni evento.
- Toda transición utiliza control de versión. Si otra operación modificó el despacho, se responde `409 Conflict` con el estado vigente.
- Los eventos conservan el mismo `idEvento` en cada reintento.
- Ningún fallo al enviar eventos revierte una transición ya confirmada.

## 10. Flujos de referencia

### 10.1. Cotización

```text
Marketplace/Chatbot -> Seguridad: client_credentials + scopes solicitados
Seguridad -> Marketplace/Chatbot: token de servicio
Marketplace/Chatbot -> Despacho: destino + líneas (SKU, cantidad) + Bearer token
Despacho -> Seguridad: client_credentials de modulo-despacho
Seguridad -> Despacho: token de servicio para Productos
Despacho -> Productos: consulta física en lote + Bearer token propio
Productos -> Despacho: peso + dimensiones por SKU
Despacho -> Despacho: calcula totales, cobertura, tarifa y plazo
Despacho -> PostgreSQL de Gestión: confirma cotización exacta e instantánea de tarifa
Despacho -> Marketplace/Chatbot: cotización
```

### 10.2. Creación y entrega

```text
Ventas -> Despacho: pedido pagado y preparado + destinatario + destino + líneas (SKU, cantidad)
Despacho -> Productos: consulta física en lote por SKU
Productos -> Despacho: peso y dimensiones por SKU
Despacho: calcula peso/volumen, registra cantidadPaquetes=1 y crea PENDIENTE_ASIGNACION
Despacho -> Ventas: idDespacho + estado PENDIENTE_ASIGNACION
Gestor -> Despacho: asignar repartidor
Despacho -> Operación: reservar capacidad y proyectar ruta
Repartidor -> Operación: iniciar/entregar/fallar
Operación -> Despacho: solicitar transición
Despacho -> Ventas: evento asíncrono de estado
```

### 10.3. Seguimiento

```text
Chatbot/Marketplace -> Seguridad: obtiene token de servicio
Chatbot/Marketplace -> Despacho: GET seguimiento por idPedido
Despacho -> Canal: estado, fecha, distrito e hitos sin coordenadas ni PII
```

## 11. Acuerdos externos que aún deben confirmarse

El contrato funcional con Ventas y Postventa está cerrado: se confirmaron el momento de creación, el payload sin `cantidadPaquetes`, la cancelación, los siete eventos, la ruta y el scope del webhook y la convención de `Idempotency-Key`. Solo quedan el aprovisionamiento o verificación de credenciales por ambiente y las pruebas de contrato.

| Equipo | Acuerdo pendiente |
|---|---|
| Seguridad y Usuarios | Confirmar el acceso humano propuesto en §8.6 (ruta, cuentas de prueba, renovación del token); registrar los scopes de `api-despacho` y asignarlos a cada `client_id`; en particular, incorporar `despachos:cancelar` para `modulo-ventas` si aún conserva la configuración anterior y verificar `ventas:pedidos:estado` para `modulo-despacho` con `aud=api-ventas`; confirmar el valor de `roles` para la cuenta del repartidor; definir el canal de alta y baja global, la comunicación de `usuario_id`, la entrega de credenciales y el contrato RabbitMQ de eventos de usuario |
| Productos y Ofertas | H-G2-01/02/03 y semántica D-G2-01/02 cerrados. Sincronizar el OpenAPI con el acuerdo y fijar su revisión; verificar el consumidor G2, coordinar con Seguridad la concesión efectiva y ejecutar las pruebas de contrato y el recorrido real de §11.1 |
| Marketplace y Chatbot | Registrar sus clientes técnicos con `cotizaciones:calcular` y `seguimientos:leer`; custodiar el `client_secret`; usar solamente `idPedido` para seguimiento |
| Equipo de Despacho | Tiempo de caché; límites de cotización; expiración de URLs firmadas y estrategia final para eventos internos |

<a id="111-respuesta-propuesta-y-plan-de-homologación-física-g2g6"></a>

### 11.1. Homologación física G2–G6 y precisiones de seguimiento

**Actualización documental:** 04/11/2026. **Hito 4 oficial:** 07/11/2026. H-G2-01/02/03 quedaron cerrados por aceptación final de G6 el 02/11/2026, después de la respuesta G2 del 26/10/2026. El PR documental G6 #108 incorporó la línea base OpenAPI `0.6.0-rc.3`, commit `40b566976fb94d924116dcd01d41b3276c2bc9e3` [cite: 1, 3].

El issue #15 se cerró el 02/11 y se reabrió el 03/11 para comunicar las precisiones siguientes; esto no reabre los acuerdos H. En ese mismo hilo, G6 ratificó D-G2-01 y aceptó el detalle D-G2-02 el 03/11; G2 confirmó precedencia y orden el 04/11, cerrando el acuerdo semántico D-G2-02. El cierre se acredita mediante esos comentarios, independientemente del estado administrativo del issue [cite: 7, 8].

| Punto | Estado contractual | Alcance y siguiente acción |
|---|---|---|
| H-G2-01 — Datos físicos incompletos | Cerrado el 02/11/2026 | `422 DATOS_FISICOS_INCOMPLETOS` con `details.skusAfectados` obligatorio para ese error; `503` técnico diferenciado. Traducción en §5.2.2. |
| H-G2-02 — Estados comerciales | Cerrado el 02/11/2026 | Enum `ACTIVO`/`INACTIVO`/`BORRADOR`, distinción de inexistencia y políticas diferentes de F-01/F-02. Un borrador no garantiza perfil válido. |
| H-G2-03 — Integridad por lote | Cerrado el 02/11/2026 | 1–100 SKU únicos/opacos, clasificación íntegra y exclusiva en `200`, cantidades conservadas y errores propios de G2. |
| D-G2-01 — Estado efectivo | Cerrado bilateralmente el 03/11/2026 | Semántica en §5.2.3; implementación del proveedor fusionada. Sincronización normativa y regresiones G2 por acreditar. |
| D-G2-02 — SKU padre no físico | Acuerdo semántico cerrado el 04/11/2026 | Campo obligatorio, afectados, orden, traducción pública y precedencia en §5.2.4. Implementación del proveedor fusionada; sincronización normativa y verificación G2 pendientes. |

Se mantienen sin renegociación la ruta de §5.2, unidades, propiedad de los datos, consulta HTTP sin mutaciones de inventario, identidad `modulo-despacho`, audiencia `api-productos` y scope `productos:fisicos:leer`.

Estados de verificación al preparar esta actualización:

| Dimensión | Estado y evidencia |
|---|---|
| Homologación H-G2-01/02/03 | Cerrada; aceptación y revisión fija enlazadas [cite: 1, 3]. |
| Homologación D-G2-01/02 | Semántica cerrada por aceptación final G6 del 03/11 y confirmación G2 del 04/11 [cite: 7, 8]. |
| Publicación normativa G6 | Pendiente de sincronización. OpenAPI inspeccionado en `6fcd81f9151649b94ecd736f8976630b39e97dc2`, versión `0.6.0-rc.3`: aún deja pendiente el rechazo específico del padre y solo exige el detalle para `DATOS_FISICOS_INCOMPLETOS` [cite: 9]. |
| Implementación G6 | PR backend #162 fusionado el 03/11, merge `eeaf39ad666d7bc5a11c0b2a80d9c8c4b0016414`. Código e informe del proveedor incorporan estado efectivo, todas las bases afectadas en orden de solicitud y precedencia; regresiones reportadas por G6, no ejecutadas desde este repositorio [cite: 6]. |
| Implementación G2 | Actualización del 05/11: cliente HTTP compartido, validación H/D, redondeos y escritor verificados localmente. La evidencia de ES-F01-06 distingue dobles, PostgreSQL y dependencias externas. |
| Autorización G7 | Grant, credencial y audiencia/scope efectivos por acreditar. |
| Despliegue y E2E G2 → G6 | Pendientes de evidencia propia; las pruebas de proveedor con emisor local no los sustituyen. |

Orden de seguimiento:

1. Sincronizar los contratos con D-G2-01/02 ya cerrados y fijar el commit/revisión normativa G6 que declara estado efectivo, campo y precedencia en la operación. El merge backend ya está identificado; un código en enum global no sustituye su declaración contractual. Contrastar las regresiones afectadas con la revisión exigible; no atribuir la ampliación D a la línea base rc.3 anterior.
2. En el backend de G2, revisar `CatalogoProductosStub`, su `@Primary` y los perfiles para que F-01 utilice el cliente HTTP normal. Revisar también el adaptador de F-02 seleccionado por `@Qualifier("productosProgramacion")`; compartir transporte y validación, conservando reglas comerciales por caso de uso.
3. Implementar agrupación, validación integral, traducción de errores y persistencia exacta de F-01. La tabla creada no acredita el escritor ni sus pruebas; los planes y tareas de [ES-F01-06](../especificaciones/ES-F01-06-CotizarEnvioConProductos/plan.md) y [ES-F02-01](../especificaciones/ES-F02-01-RecepcionSolicitudDespacho/plan.md) detallan los incrementos.
4. Con G7, acreditar concesión y provisión efectiva de la credencial. Verificar token ausente, inválido/vencido, audiencia incorrecta, scope ausente y correcto.
5. Ejecutar cotización y recepción reales G2 → G6. Registrar commits, entorno, resultados y límites en la evidencia SDD; dobles y documentación no acreditan E2E.

Fuentes de contraste:

- [cite: 1] [Aceptación final G6 del 02/11/2026](https://github.com/Taller-SW-Web/G2-Despacho-Specs/issues/15#issuecomment-6590605235), posterior a la [respuesta inicial G2 del 26/10/2026](https://github.com/Taller-SW-Web/G2-Despacho-Specs/issues/15#issuecomment-6422095659).
- [cite: 2] [DEC-F-011 y DEC-F-012 de G6](https://github.com/Taller-SW-Web/G6-Productos-Ofertas-docs/blob/40b566976fb94d924116dcd01d41b3276c2bc9e3/arquitectura/decisiones/REGISTRO_DEC_F.md), propiedad del perfil físico y distinción entre contrato, concesión de permisos e implementación.
- [cite: 3] [PR documental G6 #108](https://github.com/Taller-SW-Web/G6-Productos-Ofertas-docs/pull/108), fusionado el 02/11/2026; [OpenAPI `0.6.0-rc.3` fijo](https://github.com/Taller-SW-Web/G6-Productos-Ofertas-docs/blob/40b566976fb94d924116dcd01d41b3276c2bc9e3/contratos/http/openapi.yaml) y [registro del acuerdo](https://github.com/Taller-SW-Web/G6-Productos-Ofertas-docs/blob/40b566976fb94d924116dcd01d41b3276c2bc9e3/integraciones/ACUERDO_G2_G6_H4_2026-11-02.md).
- [cite: 4] [Propuestas D-G2-01/02 de G6 del 03/11/2026](https://github.com/Taller-SW-Web/G2-Despacho-Specs/issues/15#issuecomment-6608328544).
- [cite: 5] [Respuesta publicada de G2 del 03/11/2026](https://github.com/Taller-SW-Web/G2-Despacho-Specs/issues/15#issuecomment-6612661159), aceptación de estado efectivo y solicitud de SKU afectados.
- [cite: 6] [PR backend G6 #162](https://github.com/Taller-SW-Web/G6-Productos-Ofertas-backend/pull/162), fusionado el 03/11/2026; [código físico](https://github.com/Taller-SW-Web/G6-Productos-Ofertas-backend/blob/eeaf39ad666d7bc5a11c0b2a80d9c8c4b0016414/apps/catalog-svc/src/catalog-query.repository.ts) e [informe del proveedor](https://github.com/Taller-SW-Web/G6-Productos-Ofertas-backend/blob/eeaf39ad666d7bc5a11c0b2a80d9c8c4b0016414/apps/catalog-svc/CONSULTA-FISICA-H4.md) fijados al merge `eeaf39a`. Código inspeccionado y pruebas reportadas por G6, no ejecutadas desde este repositorio.
- [cite: 7] [Aceptación G6 del 03/11/2026](https://github.com/Taller-SW-Web/G2-Despacho-Specs/issues/15#issuecomment-6616780174), ratificación D-G2-01, aceptación del detalle D-G2-02 y propuesta final de precedencia.
- [cite: 8] [Confirmación final G2 del 04/11/2026](https://github.com/Taller-SW-Web/G2-Despacho-Specs/issues/15#issuecomment-6632557247), aceptación de precedencia y orden, traducción y cierre semántico D-G2-02.
- [cite: 9] [OpenAPI G6 inspeccionado el 04/11/2026](https://github.com/Taller-SW-Web/G6-Productos-Ofertas-docs/blob/6fcd81f9151649b94ecd736f8976630b39e97dc2/contratos/http/openapi.yaml), operación `consultarDatosFisicos` y esquema `DatosFisicosSku`; versión publicada `0.6.0-rc.3`, pendiente de sincronización D-G2-01/02.
