# Entorno de desarrollo

Cómo configurar el backend en una máquina nueva y cómo se comparten los valores
sin filtrarlos. Regla base:

> **Lo que no es secreto se versiona. Lo que es secreto va al vault del equipo.**

Nunca se commitea un `.env`, ni se pega una credencial en un issue, un PR o un
chat. Si una se expone, se rota: no basta con borrar el mensaje.

## Puesta en marcha (5 pasos)

```bash
# 1. PostgreSQL local
docker run -d --name chatbot-pg -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=chatbot -p 5432:5432 postgres:18

# 2. Entorno: la plantilla funciona tal cual, sin secretos
cp .env.template .env

# 3. Dependencias
python -m venv .venv
.venv\Scripts\activate        # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

# 4. Esquema
alembic upgrade head

# 5. Servidor (uvicorn carga el .env; python-dotenv viene con uvicorn[standard])
uvicorn src.infrastructure.main:app --reload --env-file .env
```

`alembic` carga el `.env` por su cuenta (`src/infrastructure/db/migrations/env.py`),
así que no hace falta exportar `DATABASE_URL` a mano. `load_dotenv` no sobreescribe
variables ya exportadas: en CI o en un contenedor manda el entorno real.

Con `MODULES_MODE=fake` (valor de la plantilla) el backend no llama a G2, G5, G6
ni G7: todo funciona contra adaptadores en memoria. **No hace falta ningún
secreto para desarrollar**, salvo que quieras probar el chat con LLM real.

## Qué es secreto y quién lo entrega

| Variable | ¿Secreta? | Quién la provee | Dónde vive |
|---|---|---|---|
| `DATABASE_URL` | Solo si apunta a Supabase/Railway | Cada dev (local) | `.env` local |
| `LLM_API_KEY` | **Sí** | Lead del proyecto / cuenta del equipo | Vault del equipo |
| `SECURITY_CLIENT_SECRET` | **Sí** | Dueño del módulo Seguridad (G7) | Vault del equipo |
| `SMTP_PASSWORD` | **Sí** | Buzón de Mailtrap del equipo | Vault del equipo |
| `NEXT_PUBLIC_API_BASE_URL` (frontend) | No | — | `.env.example` |
| `API_PROXY_TARGET` (frontend) | No | — | `.env.example` |
| `SONAR_TOKEN` | **Sí** | Admin de SonarCloud | GitHub → Secrets → Actions |
| `RABBITMQ_URL` | **Sí** (contiene credenciales) | Lead/DevOps (CloudAMQP) | Railway/Vault; nunca en el repo |

El resto (URLs, flags, modelos, timeouts) está en `.env.template` y se versiona.

## Perfiles de módulos

La plantilla trae dos bloques; se elige uno comentando el otro.

| Perfil | Para qué | Valores |
|---|---|---|
| **Local fake** (por defecto) | Desarrollar sin depender de otros equipos | `MODULES_MODE=fake` y `*_MODE=fake` |
| **Real** | Probar contra G7, G6, G2 y G5 desplegados | bloque "Perfil REAL" de la plantilla |

Para homologar Productos con su contrato real (acuerdo A17), sigue
[`g6-homologacion.md`](./g6-homologacion.md): exige `PRODUCTS_AUTH_MODE=service`,
token con audiencia `api-productos` y el scope de cada operación.

Valores no secretos del perfil real (ya están en la plantilla):

```
SECURITY_BASE_URL=https://g7-auth-service.onrender.com/api/v1
SECURITY_EXPECTED_ISSUER=https://g7-auth-service.onrender.com/api/v1/auth
PRODUCTS_BASE_URL=https://api-gateway.calmgrass-aec0b33c.chilecentral.azurecontainerapps.io/api/v1
DISPATCH_BASE_URL=https://taller-api-gateway.onrender.com/api/v1
SALES_MODE=http
SALES_BASE_URL=https://g5-servicio-ventas.onrender.com
POSTVENTA_BASE_URL=https://g5-ventas-postventas-backend.onrender.com
PRODUCTS_AUTH_MODE=public
ALLOW_PUBLIC_PRODUCTS=true
SECURITY_TIMEOUT_SECONDS=30
PRODUCTS_TIMEOUT_SECONDS=10
SALES_TIMEOUT_SECONDS=180
POSTVENTA_TIMEOUT_SECONDS=180
DISPATCH_TIMEOUT_SECONDS=15
```

**G5 ya está desplegado para el chatbot.** M1 (pedidos) usa el token del cliente
en las lecturas (`GET /pedidos/me`, detalle) y el token de servicio de
`modulo-chatbot` (emitido por G7) para crear, anular y notificar pagos. M2
(postventa) usa el token del cliente en todas sus operaciones (CSAT,
devoluciones y reclamos). El gateway público
`https://g5-api-gateway.onrender.com` queda detrás de Cloudflare y no se
recomienda para llamadas server-to-server; por eso el perfil real apunta a los
servicios M1/M2.

Dos detalles que cuestan tiempo si se pasan por alto:

- **`SALES_MODE=fake` explícito solo en local**. Si se omite, hereda
  `MODULES_MODE=http` y apunta a `localhost:4012`, que no existe. En local se
  mantiene `SALES_MODE=fake` para no depender de Render; en el perfil real se
  usa `SALES_MODE=http` con `SALES_BASE_URL` y `POSTVENTA_BASE_URL` (M1/M2), no
  el gateway. Ventas (G5) ya está desplegado para el chatbot.
- **`COOKIE_SECURE=false` en local**. El backend local sirve por HTTP y no queremos
  que el entorno dependa de que el navegador trate `localhost` como contexto seguro.
  En cualquier despliegue HTTPS va en `true`.

### Reloj y tokens de G7

El validador local de tokens aplica un **leeway de 60 s** (`JWT_LEEWAY_SECONDS` en
`src/shared/infrastructure/auth/jwks_token_validator.py`). G7 puede emitir `iat`
unos segundos adelantado respecto al reloj del BFF; sin ese margen, PyJWT rechaza
el token como “aún no válido” (`ImmatureSignatureError`) y el login termina en
`401 TOKEN_INVALIDO`. El margen también cubre un `exp` vencido por menos de 60 s.

Si el desfase supera los 60 s, el problema es de reloj: sincroniza NTP en G7 y en
el host del backend; no aumentes el leeway sin revisar el riesgo.

Para el perfil real solo hace falta un secreto: `SECURITY_CLIENT_SECRET`
(token de servicio de G7). `LLM_API_KEY` es independiente del perfil y solo se
necesita para probar el chat con el modelo real.

### Despliegue en Railway (G5)

Si el backend desplegado queda con `MODULES_MODE=fake` o `SALES_MODE=fake`, el
chatbot responde listas vacías y `404` para pedidos reales (lo observamos en
`g3-chatbot-backend.up.railway.app`), aunque el chat y el frontend funcionen con
los adaptadores en memoria. Para el despliegue:

```
MODULES_MODE=http
SALES_MODE=http
SALES_BASE_URL=https://g5-servicio-ventas.onrender.com
POSTVENTA_BASE_URL=https://g5-ventas-postventas-backend.onrender.com
SALES_TIMEOUT_SECONDS=180
POSTVENTA_TIMEOUT_SECONDS=180
```

No uses `https://g5-api-gateway.onrender.com` para server-to-server: queda
detrás de Cloudflare. Después de desplegar, prueba `GET /api/v1/pedidos` con un
token de cliente y confirma que devuelve pedidos reales; si ves `items: []` con
`200` y `404` en todos los detalles, revisa primero el modo (`fake` vs `http`),
no la disponibilidad de G5.

### Relay del outbox (RabbitMQ)

`OUTBOX_DISPATCH_MODE=direct` es el valor por defecto y el rollback: el worker
procesa el outbox dentro del API. Para el entregable asíncrono del Hito 5
(ADR-0030), cambia a `broker` y configura la URL de CloudAMQP en el vault:

```
OUTBOX_DISPATCH_MODE=broker
RABBITMQ_URL=amqps://<usuario>:<clave>@<host>/<vhost>
RABBITMQ_PREFETCH=10
```

`RABBITMQ_URL` lleva credenciales: no se versiona, no se pega en issues/PRs y no
se registra en logs. En modo `broker` el API publica y el consumidor de H5-M3
procesa; hasta que el consumidor esté desplegado, mantén `direct` para no dejar
correos o notificaciones a Ventas solo en las colas. Si el broker no confirma,
las filas quedan `PENDING` y se reprograman sin consumir intentos; el detalle
está en el ADR-0030.

## Cómo se comparten los secretos

1. **Vault del equipo** (Bitwarden o 1Password, plan gratuito): una entrada por
   entorno con las claves de arriba. Se da acceso por persona y se revoca al
   salir. Es la fuente de verdad para desarrollo local.
2. **GitHub → Secrets and variables → Actions**: solo para CI. Son cifrados y
   *write-only*: el workflow los inyecta, nadie puede leerlos después. No sirven
   para que un compañero configure su `.env`.
3. **GitHub Environments** (`production`) para credenciales de despliegue, con
   revisores obligatorios.
4. **Vercel / Render**: sus propias variables de entorno para los despliegues.
   No pasan por GitHub.

Alternativa si el equipo crece: Doppler o Infisical (plan gratuito) con
`infisical run -- uvicorn ...`, que inyecta el entorno sin archivos.

## Rotación

- Si un secreto se expone (chat, captura, commit): **rotarlo primero**, después
  limpiar. El historial de git y los mensajes no se pueden "deshacer".
- Antes de revocar el token de Sonar, generar uno nuevo y actualizar el secret
  `SONAR_TOKEN` del repo; si no, el análisis deja de funcionar.

## Verificación rápida

```bash
python -m pytest tests/unit -q        # 1100 tests, sin secretos ni base de datos
```

Si eso pasa, el entorno local está bien.
