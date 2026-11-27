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
| `KONG_TRUSTED_IPS` | `0.0.0.0/0,::/0` | Opcional (valor por defecto en `entrypoint.sh`). Riesgo aceptado en ADR-0031: quien llame directo a Kong puede falsificar `X-Forwarded-For`; mitigación futura: restringir origen a Vercel o exigir una cabecera secreta compartida |
| `KONG_REAL_IP_HEADER` | `X-Forwarded-For` | Opcional; conserva la IP real detrás de Vercel |

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
8. Opcional (endurecimiento): deshabilitar el dominio público directo del
   servicio FastAPI en Railway para que solo sea alcanzable por red privada
   desde Kong.

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
