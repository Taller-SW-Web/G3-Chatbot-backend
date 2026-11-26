# Medición de desfase de reloj G7 ↔ BFF

- **Fecha:** 2026-11-25
- **Solicitado por:** G7 Seguridad
- **Host de medición:** BFF G3 (reloj UTC de la instancia)
- **Alcance:** token de usuario (`/auth/login`) y token de servicio (`/auth/token`)
- **Sin tokens, contraseñas ni secretos:** solo `iat` numérico, timestamps UTC y duraciones.

## Método

1. Se solicita un token nuevo a G7 desde el host del BFF y se mide la duración
   total de la solicitud HTTP (`perf_counter`).
2. Se extrae únicamente el `iat` numérico del payload, sin imprimir el token.
3. Inmediatamente después se registra la hora UTC del BFF y se ejecuta el mismo
   `jwt.decode` que usa el BFF (RS256, issuer de G7, JWKS cacheado).
4. Se repite la validación con `leeway=0` y `leeway=60` para confirmar el efecto.
5. `offset = iat - hora UTC del BFF al validar`. Positivo = G7 adelantado.

## Muestras

### Token de usuario

| # | `iat` | `iat` UTC | BFF validación UTC | Offset (s) | Emisión (ms) | `leeway=0` | `leeway=60` |
|---|---:|---|---|---:|---:|---|---|
| 1 | 1795637755 | 2026-11-25T20:15:55Z | 2026-11-25T20:15:35.764058Z | **+19.236** | 553.6 | `ImmatureSignatureError` | OK |
| 2 | 1795637758 | 2026-11-25T20:15:58Z | 2026-11-25T20:15:38.401289Z | **+19.599** | 634.7 | `ImmatureSignatureError` | OK |
| 3 | 1795637760 | 2026-11-25T20:16:00Z | 2026-11-25T20:15:41.077386Z | **+18.923** | 674.6 | `ImmatureSignatureError` | OK |

### Token de servicio

| # | `iat` | `iat` UTC | BFF validación UTC | Offset (s) | Emisión (ms) | `leeway=0` | `leeway=60` |
|---|---:|---|---|---:|---:|---|---|
| 1 | 1795637762 | 2026-11-25T20:16:02Z | 2026-11-25T20:15:43.267916Z | **+18.732** | 189.2 | `ImmatureSignatureError` | OK |
| 2 | 1795637765 | 2026-11-25T20:16:05Z | 2026-11-25T20:15:45.528857Z | **+19.471** | 259.6 | `ImmatureSignatureError` | OK |
| 3 | 1795637767 | 2026-11-25T20:16:07Z | 2026-11-25T20:15:47.731269Z | **+19.269** | 201.1 | `ImmatureSignatureError` | OK |

## Resumen

| Métrica | Usuario | Servicio | Global |
|---|---:|---:|---:|
| Offset mínimo | +18.923 s | +18.732 s | +18.732 s |
| Offset máximo | +19.599 s | +19.471 s | +19.599 s |
| Offset medio | +19.253 s | +19.157 s | +19.205 s |
| Emisión media | 621.0 ms | 216.6 ms | 418.8 ms |
| Validación | 0.74–2.04 ms | 0.64–0.72 ms | < 2.1 ms |

## Interpretación

- Ambos tipos de token muestran el **mismo desfase**: ~18.7–19.6 s adelantados.
  No es un problema del token de usuario ni del de servicio: es el reloj de la
  instancia emisora.
- No es latencia de red: la emisión tarda 0.19–0.67 s y la validación < 2.1 ms,
  muy por debajo de los ~19 s de diferencia.
- El `iat` está truncado a segundos, así que el offset real está entre el valor
  medido y +1 s. Aun así, el rango es consistente con un adelanto de ~19–20 s.
- Con `leeway=60` ambos tokens validan; con `leeway=0` ambos fallan con
  `ImmatureSignatureError`. Confirma que el `leeway` es la mitigación correcta
  mientras G7 corrige el reloj.
- El `leeway` de 60 s sigue siendo temporal; G7 validará un token fresco sin
  tolerancia cuando el reloj esté corregido.

## Reproducción

El script de medición no forma parte del repositorio (evita credenciales). La
medición se hizo desde el host del BFF con el `.env` local y la cuenta temporal
de pruebas. Para repetirla, G7 solo necesita el `iat`, la hora UTC del BFF y la
duración de la emisión; no hacen falta tokens ni secretos.

## Segunda medición: 2026-11-26 05:38–05:39 UTC

Security reportó una emisión nueva con `iat` solo **47 ms** por delante de su reloj
sincronizado. Repetimos la medición desde el BFF con tokens frescos y `leeway=0`
solo para esta comprobación.

### Reloj del BFF contra referencia independiente

| Referencia | Offset del reloj local |
|---|---:|
| `time.cloudflare.com` (NTP) | **-0.226 a -0.231 s** |
| `time.google.com` (NTP) | **-0.232 a -0.233 s** |
| `Date` de Railway `/health` | `Thu, 26 Nov 2026 05:39:26 GMT`; UTC local 05:39:26.24–.90 → consistente dentro de 1 s |

El reloj del BFF **no** está 19 s atrás: está ~0.23 s detrás de UTC. El `Date`
del BFF desplegado en Railway también coincide con UTC dentro de la precisión de
un segundo.

### Tokens frescos y `leeway=0`

| Tipo | # | `iat` | `iat` UTC | BFF validación UTC | Emisión | Offset `iat - BFF` | `leeway=0` |
|---|---|---:|---|---|---:|---:|---|
| Usuario | 1 | 1795671524 | 05:38:44Z | 05:38:45.244590Z | 736.6 ms | -1.245 s | **OK** |
| Usuario | 2 | 1795671527 | 05:38:47Z | 05:38:47.944982Z | 696.9 ms | -0.945 s | **OK** |
| Usuario | 3 | 1795671532 | 05:38:52Z | 05:38:52.957175Z | 3010.7 ms | -0.957 s | **OK** |
| Servicio | 1 | 1795671534 | 05:38:54Z | 05:38:55.145662Z | 186.9 ms | -1.146 s | **OK** |
| Servicio | 2 | 1795671537 | 05:38:57Z | 05:38:57.329636Z | 182.8 ms | -0.330 s | **OK** |
| Servicio | 3 | 1795671539 | 05:38:59Z | 05:38:59.520668Z | 189.8 ms | -0.521 s | **OK** |

El offset negativo incluye la latencia de emisión (el `iat` se fija antes de que
llegue la respuesta); el dato relevante es que **`leeway=0` pasa en los seis
tokens**, algo imposible si G7 siguiera 19 s adelantado.

### Conclusión

- El desfase de ~19 s **ya no se reproduce** desde el BFF. Los tokens frescos
  validan sin tolerancia.
- El reloj del BFF está ~0.23 s detrás de NTP y el de Railway coincide con UTC;
  la causa no estaba en nuestro lado.
- La medición de Security (47 ms) es consistente con un G7 sano. Probablemente
  el desfase anterior era de una instancia/replica específica que ya no atiende
  estas emisiones, o fue corregido.
- Mantenemos los 60 s de tolerancia como margen temporal. Recomendamos una
  ventana de observación y luego bajarla/retirarla cuando G7 confirme que el
  reloj está estable en todas las réplicas.
- La evidencia de ~19 s de la primera medición queda como histórica; esta
  segunda medición es el estado actual.
