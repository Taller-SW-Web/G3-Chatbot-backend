# Excepciones de `pip-audit` (H5-C1)

El job **Dependency audit** de `.github/workflows/ci.yml` es bloqueante: si
`pip-audit -r requirements.txt` encuentra una vulnerabilidad conocida, el
pipeline falla y el PR no puede integrarse.

Este documento es el único lugar donde pueden vivir las excepciones aceptadas.
Cada excepción se aplica en el workflow con `--ignore-vuln <ID>`; nunca se
ignora un paquete completo ni se silencia la salida del comando. Una
vulnerabilidad nueva —de otro paquete o con otro ID— rompe el gate.

## Excepciones vigentes

**Ninguna.** El gate corre sin `--ignore-vuln`.

## Historial de excepciones retiradas

| ID | Paquete | Versión afectada | Corregido en | Retirada | Motivo del retiro |
|---|---|---|---|---|---|
| `PYSEC-2026-1845` (CVE-2025-71176 / GHSA-6w46-j5rx-g56g) | `pytest` | `<9.0.3` | 9.0.3 | 2026-11-26 (#187) | `requirements.txt` fija `pytest>=9.0.3,<10`; la suite completa pasa con pytest 9.0.3–9.2.0 y pytest-asyncio 1.5.0. Se quitó el `--ignore-vuln` del workflow y la regla `ignore` de pytest en `.github/dependabot.yml`. |

### PYSEC-2026-1845 — `pytest` (manejo de tmpdir)

- **Qué era:** pytest hasta 9.0.2 usa en UNIX directorios con el patrón
  `/tmp/pytest-of-{usuario}`; un usuario local del mismo host podía provocar
  denegación de servicio o escalada de privilegios a través de esa ruta.
- **Por qué se aceptó:** solo afectaba a pytest, herramienta de test. El
  backend no lo importa en runtime y el contenedor no ejecuta tests.
- **Cómo se retiró:** se subió `pytest` a `>=9.0.3,<10` y se mantuvo
  `pytest-asyncio>=1.5.0,<2.0` (soporte de pytest 9 desde 1.3.0; 1.4.1.post1
  eliminó el techo `pytest<9`). Con pytest 9.0.3 y 9.2.0 la suite unitaria
  completa (1590 pruebas) pasa sin el error histórico de `FixtureManager`.

## Cómo agregar una excepción

1. Confirmar que no existe una versión compatible con el fix:
   `pip-audit -r requirements.txt`.
2. Evaluar la exposición real (runtime vs. tooling, privilegios, si el vector es
   alcanzable) y documentar el control compensatorio.
3. Agregar `--ignore-vuln <ID>` al paso `Audit requirements` y una fila en
   "Excepciones vigentes" con owner y fecha de revisión (máximo 90 días).
4. Aprobación del owner de plataforma/seguridad. Sin fila en este documento, la
   excepción no existe y el gate debe quedar rojo.

## Verificación local

```bash
pip install pip-audit
pip-audit --strict -r requirements.txt
```

Resultado esperado: `No known vulnerabilities found` y código 0. El comando no
lleva `--ignore-vuln`: cualquier hallazgo debe resolverse subiendo la
dependencia o, excepcionalmente, documentando una excepción nueva aquí.
