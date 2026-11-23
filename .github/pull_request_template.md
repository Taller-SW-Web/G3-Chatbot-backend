# Resumen

<!-- Una o dos líneas: qué cambia y por qué. -->

## Spec

`Spec: openspec/specs/<capability>` — obligatorio si el diff toca `src/modules/mod_*`; el job `Specs del PR` valida que la spec exista en el repo de specs.

## Checklist

- [ ] El CI en verde (`test`).
- [ ] Tests nuevos o ajustados para el cambio.
- [ ] Sin secretos ni credenciales en el diff (`.env`, tokens, claves).
- [ ] Sin dependencias nuevas sin justificar.
- [ ] Documentación o spec actualizada si cambia comportamiento.
