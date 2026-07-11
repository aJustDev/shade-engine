# Timezone vs hora solar (frontera de la API)

## Que es

Tres nociones de "hora" que no coinciden:

- **Hora de reloj**: la de una timezone IANA (`Europe/Madrid`), que codifica
  DST y decisiones politicas. Es lo que entiende el usuario.
- **Offset fijo** (`+01:00`): una foto del desfase con UTC en un instante.
  No es una timezone: `Europe/Madrid` alterna entre +01:00 y +02:00.
- **Hora solar**: donde esta el sol de verdad. En Cordoba el mediodia solar
  cae hacia las **14:20 CEST**: Espana vive en el huso de Berlin, mas DST,
  casi dos horas de desfase con el sol. Nunca asumir "mediodia = 12:00".

## Por que lo usamos aqui

La regla del spec es "ISO 8601; sin offset se interpreta en la TZ de la
ciudad". Eso es una regla de PRESENTACION, no de geometria solar: por eso
`shade_core.solar.sun_position` rechaza datetimes naive a proposito, y la
resolucion vive en un unico punto de la API (`resolve_at` en
`shade_api.routes`):

- `at` omitido -> ahora, en la TZ de la ciudad.
- `at` naive -> se le adjunta la TZ de la ciudad (`replace(tzinfo=...)`).
- `at` con offset -> se respeta el instante y la respuesta lo devuelve
  convertido a la TZ de la ciudad (`astimezone`), para que el eco sea
  siempre coherente con los timelines locales.

La TZ de cada ciudad viene de su YAML (validada como IANA por CityConfig) y
se materializa una vez por ciudad como `ZoneInfo` en el registry.

## Trampa tipica

- El `+` de un offset ISO dentro de una query string ES UN ESPACIO por las
  reglas de URL: `?at=2026-12-21T13:20:00+01:00` llega como `...13:20:00
01:00` y da 422. Hay que enviarlo como `%2B01:00` (los clientes que
  serializan params correctamente, como httpx, lo hacen solos).
- DST: los dias de cambio tienen horas locales inexistentes o repetidas;
  `ZoneInfo` resuelve las repetidas con `fold=0` (primera ocurrencia). Para
  esta API es un caso marginal (los cambios ocurren de madrugada, con el
  sol bajo el horizonte).
- Interaccion con el cacheo HTTP: la respuesta para un `at` explicito es
  determinista y se cachea un dia; la de "ahora" implicito depende del
  reloj y va con `no-store`. En el timeline, `date=hoy` lleva
  `shaded_until` (que se mueve con el reloj) -> TTL de 60 s; cualquier
  otra fecha es determinista -> un dia.

## Referencia

- https://docs.python.org/3/library/zoneinfo.html
- Ecuacion del tiempo y mediodia solar: docs/learning/solar-geometry.md
