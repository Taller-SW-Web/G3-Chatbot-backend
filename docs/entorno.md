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

# 5. Servidor
uvicorn src.infrastructure.main:app --reload
```

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

El resto (URLs, flags, modelos, timeouts) está en `.env.template` y se versiona.

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
