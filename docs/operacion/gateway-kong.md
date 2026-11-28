# Runbook: API Gateway Kong OSS en Railway (ADR-0031, H5-G2)

Este runbook describe cómo desplegar **Kong Gateway OSS (3.x)** en modo DB-less
delante del backend FastAPI en Railway y cómo ejecutar el rollback si el gateway
falla.

## 1. Arquitectura

```text
Navegador → Vercel (Next.js /api/* via API_PROXY_TARGET)
         → Railway · Servicio Kong OSS (TLS público)
         → Railway · Servicio Backend FastAPI (red privada *.railway.internal)
```

- Configuración declarativa: `gateway/kong.yml.template` (renderizada al arrancar
  por `gateway/entrypoint.sh`).
- `KONG_DATABASE=off` y `KONG_ADMIN_LISTEN=off` (sin base de datos ni Admin API
  expuesta).
- Streaming SSE (ADR-0021): la ruta `POST /api/v1/chat/conversaciones/*/mensajes`
  desactiva `response_buffering` y fija `read_timeout: 90000` ms.
- Trazabilidad: añade `X-Request-ID` (`correlation-id`) y `X-Gateway: kong`
  (`response-transformer`).
- Publicación de OpenAPI (H5-G3): expone `GET /openapi.json`, `GET /docs` y
  `GET /redoc` a través del gateway (60 req/min por IP).
- Rate limiting perimetral (H5-G3): aplica `rate-limiting` (`policy: local`,
  `limit_by: ip`, cabeceras `RateLimit-*` y `Retry-After`) como defensa en
  profundidad sin retirar `rate_limiter.py` del backend (`application/problem+json`
  conservado):
  - Global `/api/v1`: `120/min`
  - `POST /api/v1/sesion/login`: `15/min`
  - `POST /api/v1/sesion/registro`: `5/min`, `30/hora`
  - `POST /api/v1/chat/conversaciones/*/mensajes`: `25/min`
  - `POST /api/v1/chat/conversaciones/*/adjuntos` y `POST /api/v1/evidencias`: `15/min`
  - `POST /api/v1/mensajes/{message_id}/reaccion`: `35/min`
  - `POST /api/v1/sugerencias`: `10/hora`

## 2. Despliegue en Railway

1. En el proyecto de Railway del backend, crear un nuevo servicio conectado al
   repositorio `G3-Chatbot-backend` (rama `development`).
2. Configurar **Root Directory** en `gateway` y **Dockerfile Path** en
   `Dockerfile` (relativo al Root Directory; no poner `gateway/Dockerfile` si
   Root Directory ya es `gateway`).
3. Configurar las variables de entorno del servicio Kong:

| Variable | Valor | Notas |
|---|---|---|
| `BACKEND_UPSTREAM_URL` | `http://<servicio-backend>.railway.internal:8000` | Obligatoria (fail-closed si falta). Ajustar el puerto si el backend usa otro |
| `GATEWAY_SHARED_SECRET` | `<secreto aleatorio >= 32 bytes>` | **Obligatoria en producción (#256).** Kong elimina cualquier `X-Gateway-Secret` entrante del cliente e inyecta este valor hacia FastAPI; configurar el mismo secreto en el servicio FastAPI para rechazar tráfico directo con `403 GATEWAY_REQUIRED` (`/health` queda exento para el healthcheck del contenedor/Railway) |
| `KONG_TRUSTED_IPS` | `10.0.0.0/8,100.64.0.0/10,172.16.0.0/12,192.168.0.0/16,fd00::/8` | Valor por defecto en `entrypoint.sh` (rangos privados / red interna de Railway; **nunca** `0.0.0.0/0` ni `::/0`). Evita que un cliente falsifique `X-Forwarded-For` para evadir el rate limit |
| `KONG_REAL_IP_HEADER` | `X-Forwarded-For` | Opcional; conserva la IP real detrás del proxy de Railway/Vercel |

> Railway inyecta `$PORT` automáticamente y provee TLS en el dominio público del
> servicio (`https://<gateway>.up.railway.app`).

4. Configurar el **Healthcheck Path** del servicio Kong en `/health`.
5. Generar el dominio público de Kong y verificar antes de cambiar Vercel:

```bash
curl -i https://<gateway>.up.railway.app/health
```

Debe responder `HTTP/2 200` con `x-gateway: kong` y `x-request-id`.

6. En **Vercel** (entornos **Production** y **Preview** de `G3-Chatbot-frontend`),
   actualizar `API_PROXY_TARGET=https://<gateway>.up.railway.app` y redesplegar.
7. Verificar desde el dominio de Vercel que `/api/v1/catalogo/productos` responde
   `200` con `x-gateway: kong` y que un turno de chat transmite SSE en vivo.
8. **Obligatorio en producción (#256 — protección del backend):**
   - Deshabilitar el dominio público directo (`*.up.railway.app`) del servicio
     FastAPI en Railway para que solo sea alcanzable por red privada desde Kong,
     **y/o** configurar el mismo `GATEWAY_SHARED_SECRET` en ambos servicios (Kong
     y Backend FastAPI) para que cualquier petición directa sin `X-Gateway-Secret`
     reciba `403 GATEWAY_REQUIRED`.
   - Verificar que un `X-Forwarded-For` falsificado no evade el rate limit de Kong
     (cubierto en `tests/integration/test_gateway_kong.py`).

## 3. Rollback

Si el gateway presenta latencia anómala, caídas o problemas de streaming:

1. Si el dominio público del backend FastAPI estaba deshabilitado en Railway,
   volver a habilitarlo (o usar su dominio de respaldo).
2. En **Vercel**, cambiar `API_PROXY_TARGET` de vuelta a
   `https://<backend>.up.railway.app` en Production y Preview y redesplegar.
3. Verificar `GET https://<frontend>/api/v1/catalogo/productos` (`200`) y un turno
   de chat SSE.
4. No hace falta revertir código ni migraciones en el backend.

## 4. Prueba local / CI

```bash
docker build -f gateway/Dockerfile -t g3-chatbot-gateway:local gateway
docker run --rm --network host \
  -e PORT=8080 \
  -e BACKEND_UPSTREAM_URL=http://127.0.0.1:8000 \
  g3-chatbot-gateway:local
```

Con el backend corriendo en `127.0.0.1:8000`:

```bash
GATEWAY_TEST_URL=http://127.0.0.1:8080 pytest tests/integration/test_gateway_kong.py -v
```
