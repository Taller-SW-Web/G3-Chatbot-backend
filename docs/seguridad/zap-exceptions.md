# Excepciones de DAST — OWASP ZAP (H5-C2)

El job **DAST (OWASP ZAP)** de `.github/workflows/ci.yml` levanta PostgreSQL 18
y el backend FastAPI en modo controlado (`MODULES_MODE=fake` y
`LLM_PROVIDER=fake`, sin datos reales ni secretos) y ejecuta:

1. `zap-api-scan.py` en modo pasivo (`-S`) sobre `http://127.0.0.1:8000/openapi.json`
   para cubrir todos los endpoints declarados en el contrato OpenAPI.
2. `zap-baseline.py` sobre `http://127.0.0.1:8000/health`.

Los reportes completos (`HTML`, `JSON` y `Markdown`) se publican como artefacto
`dast-zap-report` en cada corrida y el resumen se muestra en el Step Summary.

## Política de bloqueo

El script `scripts/check_zap_report.py` evalúa los reportes JSON generados:

- **High (`riskcode == "3"`):** **bloqueante**. Si aparece cualquier alerta
  High sin excepción explícita en `.zap/rules.tsv`, el job falla y bloquea el
  merge.
- **Medium (`2`), Low (`1`) e Informational (`0`):** informativos en el
  reporte y en el resumen; no bloquean el merge (por ejemplo, cabeceras HTML
  como CSP o `X-Frame-Options` que en esta arquitectura aplica el frontend en
  Vercel según ADR-0023).

## Excepciones vigentes

| Plugin ID | Alerta | Severidad | Motivo | Control compensatorio | Owner | Revisión |
|---|---|---|---|---|---|---|
| *(ninguna)* | — | — | — | — | — | — |

## Cómo agregar una excepción

1. Confirmar en el artefacto `dast-zap-report` que el hallazgo High es un falso
   positivo o está mitigado por diseño.
2. Agregar `<plugin_id>\tIGNORE\t<motivo breve>` en `.zap/rules.tsv`.
3. Registrar una fila en la tabla **Excepciones vigentes** con motivo, control
   compensatorio, owner y fecha de revisión (máximo 90 días).
4. Solicitar aprobación del owner de plataforma/seguridad en el PR. Sin fila en
   este documento, la excepción no debe aceptarse.
