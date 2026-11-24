# Pruebas de carga con k6 (H5-P2 y H5-P3)

Scripts de k6 que miden el backend frente a los SLO de
[`G3-Chatbot-specs/docs/slo-performance.md`](https://github.com/Taller-SW-Web/G3-Chatbot-specs/blob/main/docs/slo-performance.md).
Cada SLO es un *threshold*: si uno no se cumple, k6 termina con código distinto de cero.

| Script | Flujo | Carga | Endpoints |
|---|---|---|---|
| `catalogo.js` | `mod-catalog` | 20 solicitudes/s | búsqueda, detalle, promociones, disponibilidad |
| `chat.js` | `mod-chat` | 10 clientes anónimos, un mensaje cada 3 a 5 s | crear conversación, turno SSE, acción directa, historial |
| `checkout.js` | `mod-cart` + `mod-checkout` | 1 cliente que compra en bucle | agregar, ver carrito, crear checkout, pagar |

## Reglas

- **Nunca contra producción ni CloudAMQP.** Railway tiene un solo entorno y la carga agota la cuota
  del broker (`hito-5.md`, restricciones). `BASE_URL` apunta al stack local o de CI.
- El backend corre con `MODULES_MODE=fake` (la plantilla `.env.template`) y `LLM_PROVIDER=fake`
  (`ScriptedLLMProvider`): sin red y sin secretos.
- Si cambias un SLO, cámbialo primero en el documento de specs y después en `lib/common.js`.

## Correr en local

```bash
# 1. PostgreSQL y esquema (docs/entorno.md); con LLM_PROVIDER=fake en el .env
cp .env.template .env && sed -i 's/^LLM_PROVIDER=.*/LLM_PROVIDER=fake/' .env
alembic upgrade head

# 2. Backend
uvicorn src.infrastructure.main:app --host 0.0.0.0 --port 8000 --env-file .env

# 3. k6 en Docker (no hace falta instalarlo); en Linux usa --network host y 127.0.0.1
cd tests/performance/k6 && mkdir -p results
docker run --rm -v "$PWD:/scripts" -w /scripts \
  -e BASE_URL=http://host.docker.internal:8000 -e RESULTS_DIR=/scripts/results \
  grafana/k6:2.4.0 run catalogo.js
```

Variables: `BASE_URL`, `DURATION` (por defecto `2m`), `RESULTS_DIR`, `CATALOG_RATE`, `CHAT_VUS`,
`CHAT_MIN_RATE` y `CHECKOUT_MIN_RATE`. Cada corrida deja `<flujo>-report.md` (tabla SLO frente a lo
medido) y `<flujo>-summary.json` en `results/`, que está en `.gitignore`.

## En GitHub Actions

`Actions → Performance (k6) → Run workflow` (`.github/workflows/performance.yml`). Levanta PostgreSQL
y el backend en el runner, corre los tres escenarios, publica la tabla en el resumen del job y sube
`results/` como artefacto `k6-results`. El job falla si algún SLO no se cumple.

## Limitaciones conocidas

- **Checkout en serie.** El fake de Seguridad tiene un solo cliente activo (`cliente@demo.pe`) y el
  carrito es uno por cliente. Medir compras en paralelo exige sembrar más clientes en el fake, un
  cambio en `src/shared/` que va en su propio PR `cross-modulo`.
- **Detalle de producto.** Solo se piden los productos semilla con precio validado; el resto responde
  `503` a propósito (imita los productos de G6 sin precio de canal).
- **RabbitMQ.** Cuando entren H5-M2 y H5-M3, el workflow suma el servicio de RabbitMQ en Docker y el
  consumidor; hasta entonces el outbox trabaja solo, igual que en el resto del CI.
- **LLM.** Con el modelo real hay que sumar la latencia de OpenAI, que estos SLO no controlan.
