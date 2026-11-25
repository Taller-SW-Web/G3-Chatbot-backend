# Excepciones de `pip-audit` (H5-C1)

El job **Dependency audit** de `.github/workflows/ci.yml` es bloqueante: si
`pip-audit -r requirements.txt` encuentra una vulnerabilidad conocida, el
pipeline falla y el PR no puede integrarse.

Este documento es el único lugar donde viven las excepciones aceptadas. Cada
excepción se aplica en el workflow con `--ignore-vuln <ID>`; nunca se ignora un
paquete completo ni se silencia la salida del comando. Una vulnerabilidad nueva
—de otro paquete o con otro ID— sigue rompiendo el gate.

## Excepciones vigentes

| ID | Paquete | Versión resuelta | Corregido en | Motivo | Control compensatorio | Owner | Revisión |
|---|---|---|---|---|---|---|---|
| `PYSEC-2026-1845` (CVE-2025-71176 / GHSA-6w46-j5rx-g56g) | `pytest` | 8.4.2 (`pytest>=8.0,<9.0`) | 9.0.3 | El fix exige pytest 9, un salto mayor fuera del alcance de H5-C1. `requirements.txt` mantiene el techo `<9.0` y Dependabot ignora ese salto mientras se valida la suite. | pytest no se ejecuta en producción; el contenedor corre como `appuser` (uid 10001) y los runners de CI son efímeros. `dependency-review` bloquea severidad alta y Trivy escanea CRITICAL. | `@Taller-SW-Web/chatbot` | 2027-01-31 |

### PYSEC-2026-1845 — `pytest` (manejo de tmpdir)

- **Qué es:** pytest hasta 9.0.2 usa en UNIX directorios con el patrón
  `/tmp/pytest-of-{usuario}`; un usuario local del mismo host puede provocar
  denegación de servicio o escalada de privilegios a través de esa ruta.
- **Por qué se acepta:** solo afecta a pytest, herramienta de test. El backend
  no lo importa en runtime y el contenedor no ejecuta tests: el `CMD` es
  uvicorn y corre sin privilegios. En CI cada job usa un runner efímero de
  GitHub, sin otros usuarios locales. El vector no es alcanzable por HTTP.
- **Por qué no se corrige en este PR:** subir a `pytest>=9.0.3` es un cambio de
  dependencia mayor, fuera del alcance de H5-C1, y exige validar la suite
  completa con `pytest-asyncio`. Se hará en un PR dedicado.
- **Seguimiento:** con `pytest==9.0.3` y `pytest-asyncio==1.5.0` la recolección
  de `tests/unit` (1423 pruebas) ya no muestra el error histórico de
  `FixtureManager`; el bump queda pendiente para validar la ejecución completa
  e integración en ese PR de dependencias.
- **Condición de retiro:** cuando `requirements.txt` fije `pytest>=9.0.3`,
  quitar `--ignore-vuln PYSEC-2026-1845` del workflow, borrar esta fila y
  eliminar la regla `ignore` de pytest en `.github/dependabot.yml` en el mismo
  PR.

## Cómo agregar una excepción

1. Confirmar que no existe una versión compatible con el fix:
   `pip-audit -r requirements.txt`.
2. Evaluar la exposición real (runtime vs. tooling, privilegios, si el vector es
   alcanzable) y documentar el control compensatorio.
3. Agregar `--ignore-vuln <ID>` al paso `Audit requirements` y una fila aquí con
   owner y fecha de revisión (máximo 90 días).
4. Aprobación del owner de plataforma/seguridad. Sin fila en este documento, la
   excepción no existe y el gate debe quedar rojo.

## Verificación local

```bash
pip install pip-audit
pip-audit --strict -r requirements.txt \
  --ignore-vuln PYSEC-2026-1845
```

Resultado esperado: `No known vulnerabilities found, 2 ignored` y código 0.
El contador puede variar si pip-audit reporta el mismo ID por más de una ruta de
resolución. Sin `--ignore-vuln`, el comando debe fallar con
`PYSEC-2026-1845`.
