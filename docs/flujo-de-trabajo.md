# Flujo de trabajo — de la spec al PR por módulo

Este documento describe cómo entra un cambio en el backend sin romper el
monolito modular (ADR-0024). Aplica a humanos y agentes.

## 0. Entradas

| Fuente | Qué aporta |
|---|---|
| `G3-Chatbot-specs/openspec/specs/<capacidad>/spec.md` | comportamiento y escenarios. |
| `G3-Chatbot-specs/openspec/specs/<capacidad>/design.md` | contratos, endpoints y tablas. |
| `modules.manifest.json` | módulo dueño, DAG y owners. |
| `src/modules/<mod>/MODULE.md` | rutas, superficie pública, dependencias y pruebas. |
| `docs/adr/ADR-0024-modularizacion.md` | reglas de frontera. |

Si falta la spec o una decisión de producto, el flujo se detiene y se reporta.
No se implementa “lo que parece razonable”.

## 1. Especificar

1. Identifica la capacidad y el módulo dueño en el manifiesto.
2. Lee la spec completa: requisitos, escenarios, requisitos no funcionales y
   criterios de completitud.
3. Comprueba si el cambio ya está cubierto. Si no:
   - abre una issue en `G3-Chatbot-specs` asignada a `@Ankarmin` y `@MathiasTL`;
     no abras ramas ni PRs en specs;
   - registra en el PR qué escenario nuevo se añadió y por qué; el PR espera a
     que la spec esté en `development`.
4. Si el cambio afecta el DAG o una capacidad, escribe la decisión: qué módulo
   la registra, quién la consume y si es requerida u opcional.

**Salida:** spec actualizada + lista de archivos previstos + criterio de
aceptación verificable.

## 2. Diseñar el cambio dentro del módulo

1. ¿Es una regla pura? → `domain/`.
2. ¿Es orquestación de puertos? → `application/`.
3. ¿Es un contrato nuevo? → `ports/` (del módulo) o
   `src/shared/application/ports/` si es transversal.
4. ¿Es I/O? → `adapters/` (HTTP, cliente, PostgreSQL, fake).
5. ¿Otro módulo lo necesita? → `public.py` del dueño + `dependsOn` en el
   manifiesto. Nunca importar su interior.
6. ¿El DAG lo prohíbe? → capacidad en
   `src/shared/infrastructure/di/capabilities.py`, registrada por el dueño en
   `configure()` y resuelta por el consumidor.

Reglas de capa:

- `application/` no importa FastAPI, httpx, SQLAlchemy ni
  `shared.infrastructure.db.models`.
- `domain/` no importa framework, red ni entorno.
- `adapters/` es el único lugar con SQL, HTTP, SMTP o LLM.
- Los adapters hacen `flush`; el caso de uso controla `commit`/`rollback`.

## 3. Implementar

1. Escribe el test que falla (regla de negocio o contrato HTTP).
2. Implementa lo mínimo para pasarlo.
3. Cablea en `module.py`: providers, `build_router(options)` y `configure()`.
4. Si el módulo necesita una capacidad, declara
   `REQUIRED_CAPABILITIES`/`OPTIONAL_CAPABILITIES`; el arranque falla si falta
   una requerida y `/health` reporta las opcionales.
5. No añadas archivos vacíos ni placeholders: si no se usa, no existe.

## 4. Probar

| Nivel | Qué cubre | Ubicación |
|---|---|---|
| Unitario | reglas, casos de uso, fakes | `tests/unit/mod_<x>/` |
| Router | status, `problem+json`, auth, validación | `tests/unit/mod_<x>/test_*_router.py` |
| Integración | PostgreSQL 18 real (`TEST_DATABASE_URL`) | `tests/integration/mod_<x>/` |
| Gate | fronteras y contrato | `tests/unit/test_check_modules.py`, `tests/unit/shared/test_openapi_export.py` |

Comandos obligatorios:

```bash
pytest tests/unit -q
ruff check .
mypy src
python scripts/check_modules.py
python scripts/export_openapi.py --check
```

El job `validate` agrupa `ruff`, `mypy`, `check_modules` y la comprobación/validación del OpenAPI; `pytest` corre en el job `unit-tests`. Para reproducir `validate`:

```bash
python scripts/ci/run_validators.py
python scripts/ci/scope.py --base origin/development --event pull_request --json
```

Un PR de solo documentación no corre tests, Docker ni Sonar. La etiqueta `ci:full` fuerza el alcance completo, los jobs caros y la auditoría; añadirla relanza el workflow. Detalle y comandos de integración en [docs/ci-pipeline.md](https://github.com/Taller-SW-Web/G3-Chatbot-specs/blob/development/docs/ci-pipeline.md).

> **Windows:** `test_consumer_entrypoint.py::test_sigterm_stops_the_consumer_and_returns_cleanly` falla por `asyncio.add_signal_handler`, que no existe en Windows. Usa WSL o deselecciónalo; el CI corre en Ubuntu.

Si tocaste wiring/capacidades/discovery:

```bash
python -c "from src.infrastructure.main import app; print(len(app.routes))"
```

## 5. Actualizar el contrato

- `docs/openapi.json` se regenera con `python scripts/export_openapi.py`.
- Un endpoint nuevo o un cambio de esquema se refleja en `MODULE.md` y en los
  tests del router.
- No cambies paths/métodos existentes sin spec; los cambios aditivos son la
  norma.
- El tag del router debe ser `MODULE_ID`; el checker lo exige.

## 6. Abrir el PR/MR (uno por módulo)

Plantilla mínima:

```markdown
## Qué
[capacidad y módulo]

## Spec
[enlace y escenarios cubiertos; si se actualizó, enlace al cambio]

## Cambio
[puertos, casos, adapters, wiring]

## Verificación
- pytest tests/unit -q → N passed
- ruff check . → OK
- mypy src → OK
- python scripts/check_modules.py → OK
- python scripts/export_openapi.py --check → OK

## Riesgos / contrato
[capacidades nuevas, migraciones, cambios aditivos, rollback]

## Fuera de alcance
[lo que no se hizo y por qué]
```

Reglas:

- Un PR = un `mod_*`. Si toca `shared/`, label `cross-modulo` y revisión del
  owner de plataforma.
- Si toca dos módulos, se parte o se abre primero el PR de contrato.
- No se hace push/PR a otros repos desde aquí; los cambios de spec se coordinan
  con `G3-Chatbot-specs`.

## 7. Revisión de coherencia

Antes de pedir review, el autor verifica:

- [ ] La spec existe y el cambio está en ella (o la spec se actualizó aquí).
- [ ] El módulo dueño es el correcto según el manifiesto.
- [ ] No hay imports de `domain/`, `application/` ni `adapters/` ajenos.
- [ ] `application/` no importa ORM ni adapters concretos.
- [ ] Las capacidades están declaradas y el arranque falla claro si faltan.
- [ ] Los errores usan `code` estable y `problem+json`.
- [ ] No se cambiaron paths/métodos existentes; el OpenAPI es consistente.
- [ ] Los tests cubren el escenario feliz y los bordes de la spec.
- [ ] `MODULE.md`, `modules.manifest.json` y `arquitectura-backend.md` siguen
      siendo ciertos.

El reviewer comprueba lo mismo y además:

- [ ] El PR no mezcla módulos ni deuda no relacionada.
- [ ] No hay secretos, datos personales ni PAN/CVV en logs o tests.
- [ ] La migración (si hay) es reversible y sigue
      `YYYYMMDD_mod_<id>_<desc>`.
- [ ] El rollback es posible sin tocar datos de otro módulo.

## 8. Definition of Done

Un cambio está Done cuando:

1. **Spec**: el comportamiento está en la spec y los escenarios tienen test.
2. **Código**: vive en el módulo correcto, respeta capas y no importa
   interiores ajenos.
3. **Contrato**: `docs/openapi.json` actualizado/verificado; rutas y `tags`
   correctos.
4. **Tests**: unitarios verdes; router/integración si aplica; sin tests
   borrados ni `skip` nuevos sin justificación.
5. **Gates**: `pytest`, `ruff`, `mypy`, `check_modules` y `export_openapi
   --check` en verde.
6. **Arranque**: `from src.infrastructure.main import app` funciona; si hay
   capacidades nuevas, el arranque falla claro al faltar una requerida.
7. **Documentación**: `MODULE.md` y `arquitectura-backend.md` coherentes; el PR
   explica riesgos y rollback.
8. **Revisión**: aprobada por el owner del módulo; `cross-modulo` si tocó
   `shared/`.
9. **PR único**: un solo módulo (o contrato + consumidor justificados).

## 9. Ejemplo de bloqueo (H-17)

El frontend llama `POST /api/v1/contacto/celular/verificar-otp` y la spec
`validacion-celular` define también `solicitar-otp`. El backend no los expone y
no existe almacén/adaptador de OTP (solo `PhoneVerificationPort.existe` y la
tabla `local_phone_verification` de verificaciones ya completadas). Según este
flujo, el cambio se detiene: falta la decisión de persistencia del OTP y del
envío simulado, y el frontend ni siquiera solicita el código. Se reporta con
evidencia en lugar de inventar la solución.
