# AGENTS.md — Reglas para agentes en `G3-Chatbot-backend`

Este repositorio es el BFF FastAPI del canal Chatbot: un monolito modular por
capacidad (ADR-0024) con Clean Architecture + Hexagonal dentro de cada módulo.
Antes de tocar código, lee:

1. `modules.manifest.json` (DAG y owners).
2. `src/modules/<mod>/MODULE.md` (rutas, superficie pública, dependencias).
3. La spec correspondiente en `G3-Chatbot-specs/openspec/specs/<capacidad>/`.
4. `docs/adr/ADR-0024-modularizacion.md` y `arquitectura-backend.md`.

## Regla 1 — La spec manda

- No implementes comportamiento que no esté en la spec del módulo.
- Si el cambio **no está** en la spec, no implementes: abre una issue en
  `G3-Chatbot-specs` asignada a `@Ankarmin` y `@MathiasTL`. Solo `@MathiasTL` y
  `@Ankarmin` abren PRs en specs; el resto no crea ramas ni PRs. El PR de código
  espera a que la spec esté en `development`. Si falta una
  decisión de producto, detente y repórtalo con la evidencia; no la inventes.
- Si la spec y el código se contradicen, gana la spec y el PR debe explicar la
  corrección.

## Regla 2 — Coherencia con lo existente

- Reutiliza los patrones del módulo: puertos en `ports/`, reglas puras en
  `domain/`, orquestación en `application/`, I/O en `adapters/`.
- No introduzcas un patrón nuevo si el repositorio ya tiene uno para lo mismo.
- Respeta nombres, formato de errores (`problem+json` con `code`), DTOs,
  `Idempotency-Key`, paginación y `tags=[MODULE_ID]`.
- No cambies rutas, métodos ni códigos de error existentes sin spec y sin
  actualizar `docs/openapi.json`.

## Regla 3 — Un PR/MR por módulo

- Un PR toca **un solo `mod_*`**. Si necesitas tocar dos módulos, divide el
  trabajo o abre un PR de contrato primero.
- Los cambios en `src/shared/` llevan label **`cross-modulo`** y revisión del
  owner de plataforma. No metas negocio de un módulo en `shared/`.
- Un cambio de DAG se hace en `modules.manifest.json`, en
  `docs/adr/ADR-0024-modularizacion.md` y en `arquitectura-backend.md` en el
  mismo PR.
- No toques `G3-Chatbot-frontend` ni `G3-Chatbot-specs` desde este repo; los
  cambios de spec se reportan con una issue en specs asignada a `@Ankarmin` y
  `@MathiasTL`. No abras ramas ni PRs en specs.

## Regla 4 — Fronteras

- Un módulo importa solo `src.shared.*`, su árbol y
  `src.modules.<otro>.public` para las dependencias declaradas.
- Prohibido importar `domain/`, `application/` o `adapters/` de otro módulo.
- `application/` no importa `shared.infrastructure.db.models` ni adapters
  concretos; usa puertos y DTOs de inserción.
- Si el DAG prohíbe el import, usa una capacidad del registry:
  - el dueño la registra en `configure(container)`;
  - el consumidor la resuelve en tiempo de request;
  - declara `REQUIRED_CAPABILITIES` u `OPTIONAL_CAPABILITIES` (H-08).
- Nada de `None` silencioso para una capacidad requerida.

## Regla 5 — Verificación obligatoria

En cada PR, desde `G3-Chatbot-backend`:

```bash
pytest tests/unit -q
ruff check .
mypy src
python scripts/check_modules.py
python scripts/export_openapi.py --check
```

Además:

- Test unitario de la regla/caso nuevo.
- Test de router o integración si cambió el borde HTTP.
- `python -c "from src.infrastructure.main import app"` si tocaste wiring,
  discovery o capacidades.
- Actualiza `MODULE.md` y `docs/openapi.json` si cambió la superficie pública.

## Regla 6 — Commits y revisión

- Un commit por hallazgo/cambio lógico: `fix(mod-x): ...`, `feat(mod-x): ...`,
  `docs(...)`, `test(...)`.
- No dejes tests rojos, imports muertos ni archivos de 0 bytes.
- El PR describe: spec leída, cambio, evidencia de verificación, riesgos y
  cambios de contrato. Si no hay cambio de contrato, dilo explícitamente.
- La revisión humana decide en lo irreversible (migraciones, capacidad,
  borrado de datos, cambios de DAG).
- **Cierre de issues:** un issue no se cierra hasta que el PR esté mergeado en
  `development` y verificado. El cierre cita PR, commit y evidencia; duplicado u
  obsoleto se cierra con motivo, nunca como `completed` si el trabajo sigue
  pendiente.
- **Ramas:** los PR van a `development`, rama por defecto y de producción;
  `main` es un placeholder de un commit con solo `README.md`. No promuevas ni
  despliegues desde `main`.
- **Incoherencia con la spec:** si una issue contradice la spec o pide algo no
  definido, no la implementes; abre una issue en `G3-Chatbot-specs` asignada a
  `@Ankarmin` y `@MathiasTL`, enlaza la original y espera la decisión. Regla
  completa: `G3-Chatbot-specs/AGENTS.md` §8.
- **Higiene:** no reescribas la historia compartida de `development`, no borres
  tags o ramas sin coordinar y no pegues secretos en issues o PRs.

## Atajos de lectura

| Necesitas | Lee |
|---|---|
| DAG y owners | `modules.manifest.json` |
| Reglas de un módulo | `src/modules/<mod>/MODULE.md` |
| Contrato HTTP | `docs/openapi.json` + `MODULE.md` |
| Capabilities | `src/shared/infrastructure/di/capabilities.py` |
| Errores | `src/shared/infrastructure/http/problem.py` |
| DB/modelos | `src/shared/infrastructure/db/` |
| Gate de fronteras | `scripts/check_modules.py` + `tests/unit/test_check_modules.py` |
| Flujo completo | `docs/flujo-de-trabajo.md` |
