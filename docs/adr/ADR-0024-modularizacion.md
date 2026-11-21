# ADR-0024 — Monolito modular por capacidad (backend y frontend)

- **Estado:** aceptado
- **Fecha:** 2026-11-21
- **Decisores:** equipo G3
- **Repos afectados:** `G3-Chatbot-backend`, `G3-Chatbot-frontend`

## Contexto

Los dos repos ya siguen Clean Architecture + Hexagonal (ADR-0002), pero el corte
es **por capa** (`domain/`, `application/`, `ports/`, `adapters/`). Eso hace que
cualquier feature toque los mismos archivos y carpetas que otra persona:
`main.py`, el contenedor DI, `ChatbotApiAxiosAdapter`, `app/` y el registro de
navegación. El resultado son PRs que se pisan y rebases constantes.

## Decisión

Cortar también **por capacidad de negocio**: un monolito modular con 8 módulos,
con el **mismo id y la misma cantidad** en backend y frontend.

| id | Backend | Frontend |
|---|---|---|
| `mod-auth` | sesión, registro, MFA, OTP, perfil | `/cuenta`, `/verificar-correo` |
| `mod-catalog` | productos, detalle, promos, disponibilidad | `/`, tarjetas y bloques de catálogo |
| `mod-cart` | carrito, items, cupón | `/carrito` |
| `mod-shipping` | cotización de envío y direcciones | bloque de envío en `/checkout` |
| `mod-checkout` | checkout, estado y pago | `/checkout`, `/exito` |
| `mod-orders` | pedidos, detalle, seguimiento, reenvío | `/pedidos`, `/pedidos/[tab]`, `/seguimiento` |
| `mod-postventa` | devoluciones, reclamos, evidencias, CSAT | `/devolucion`, `/reclamo` |
| `mod-chat` | turnos, mensajes, SSE, acciones | `/chat/[id]`, `/sugerencias` |

`shared/` es el kernel: configuración, DB, DI, errores, auth de transporte,
correlation id, outbox y primitivas UI. **No contiene reglas de negocio.**

## Reglas de dependencia

1. Un módulo solo importa `shared/`, su propio árbol y el `public.py` / `public.ts`
   de los módulos declarados en `dependsOn` del manifiesto.
2. Prohibido importar `domain/`, `application/`, `adapters/` de otro módulo.
3. El DAG es:

   ```text
   catalog ← cart ← checkout ← orders ← postventa
               ↑        ↑
            shipping ───┘
   chat → todos (orquestador)
   auth → usado por todos vía shared/auth
   ```

4. `chat` puede depender de todos; ningún módulo depende de `chat`.
5. Los routers conservan las rutas públicas actuales; el `basePath` de cada
   módulo se declara en su `MODULE.md` y en su manifiesto de frontend.
6. Un PR toca **un solo módulo**. `shared/` requiere label `cross-modulo` y
   revisión del owner de plataforma.

## Mecanismos

- **Auto-discovery:** `main.py` descubre `src/modules/*/module.py`; el frontend
  genera su registry desde los `module.manifest.ts`. Nadie edita una lista
  compartida para añadir su módulo.
- **Contrato:** cada router lleva `tags=["mod-x"]`; el backend publica
  `openapi.json` y el frontend valida que los endpoints que usa cada módulo
  existan y estén etiquetados con el mismo id.
- **Gates:** `import-linter` (backend), `dependency-cruiser`/ESLint (frontend),
  `scripts/check_modules.*` (paridad de manifiestos y estructura) y `pr-scope`
  (un módulo por PR).
- **Migraciones:** cadena única de Alembic con nombre
  `YYYYMMDD_mod_<id>_<desc>`; una rama por módulo queda como opción futura.

## Consecuencias

- **Positivas:** PRs pequeños y paralelos, sin rebase entre módulos; ownership
  claro; el contrato backend↔frontend se verifica en CI.
- **Negativas:** mover código una vez (strangler por módulo); `shared/` exige
  disciplina para no convertirse en cajón de sastre; Next obliga a mantener
  archivos wrapper en `app/`.
- **Neutras:** no se introducen microservicios, repos nuevos ni cambios de URL.

## Migración

Fase 0 manifiesto + ADR + plantilla; Fase 1 `shared/`; Fase 2 backend por
módulo; Fase 3 frontend por módulo; Fase 4 gates en bloqueante y CODEOWNERS.
Cada fase deja `pytest`, `ruff`, `mypy`, `lint`, `typecheck` y `vitest` en verde.
