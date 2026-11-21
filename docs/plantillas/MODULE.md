# `mod-xx` — Nombre del módulo

- **Owner:** @usuario
- **Backend:** paquete `mod_xx`, `basePath` `/ruta`
- **Frontend:** carpeta `mod-xx`, rutas `[/ruta]`
- **Depende de:** `mod-yy` (solo por `public.py` / `public.ts`)
- **Contrato:** endpoints y eventos que expone

## Responsabilidad

Una frase: qué capacidad de negocio resuelve y qué **no** incluye.

## Superficie pública

| Tipo | Archivo | Qué expone |
|---|---|---|
| Backend | `public.py` | Protocolos, DTOs y errores que otros módulos pueden usar |
| Frontend | `public.ts` | Tipos y componentes que otros módulos pueden importar |

## Rutas

| Método | Ruta | Caso de uso |
|---|---|---|

## Dependencias externas

Módulos G1–G7, servicios o colas que consume (con su contrato).

## Pruebas

- Unitarias: `tests/unit/mod_xx/`
- Integración: `tests/integration/mod_xx/`
- Contrato: endpoint ↔ schema del frontend

## Reglas

- No importar `domain/`, `application/` ni `adapters/` de otro módulo.
- No tocar `shared/` sin label `cross-modulo`.
- Un PR de este módulo no modifica archivos de otro `mod-*`.
