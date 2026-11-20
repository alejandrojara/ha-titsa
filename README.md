# TITSA para Home Assistant (`ha-titsa`)

[![CI](https://github.com/alejandrojara/ha-titsa/actions/workflows/ci.yml/badge.svg)](https://github.com/alejandrojara/ha-titsa/actions/workflows/ci.yml)
[![hassfest](https://github.com/alejandrojara/ha-titsa/actions/workflows/hassfest.yml/badge.svg)](https://github.com/alejandrojara/ha-titsa/actions/workflows/hassfest.yml)
[![HACS](https://github.com/alejandrojara/ha-titsa/actions/workflows/hacs.yml/badge.svg)](https://github.com/alejandrojara/ha-titsa/actions/workflows/hacs.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Integración personalizada **no oficial** para **Home Assistant** que muestra los tiempos de llegada en tiempo real de las guaguas de **TITSA (Tenerife)** combinando el catálogo oficial de **Datos Abiertos del Cabildo de Tenerife (CC BY 4.0)** con las estimaciones en vivo por parada.

> [!IMPORTANT]
> **Componente no oficial:** Este proyecto es una integración comunitaria independiente desarrollada por terceros. **No está afiliado, patrocinado, avalado ni soportado oficialmente por Transportes Interurbanos de Tenerife, S.A. (TITSA) ni por el Cabildo Insular de Tenerife.** Para soporte o incidencias técnicas de esta integración utiliza únicamente el [issue tracker de este repositorio](https://github.com/alejandrojara/ha-titsa/issues) y nunca los canales de atención al cliente de TITSA.

> **Fase 2 (tarjeta personalizada estilo marquesina):** [`alejandrojara/ha-titsa-card`](https://github.com/alejandrojara/ha-titsa-card) *(próximamente)*. Esta integración funciona por sí sola con tarjetas nativas de Home Assistant (ejemplos incluidos abajo).

---

## Características principales

* **Configuración 100 % visual (sin YAML):** entrada padre única (`TITSA`) + subentradas por parada (`Añadir parada`) con soporte completo de **reconfiguración**.
* **Búsqueda rápida de paradas y líneas:**
  * Descarga inmediata de los catálogos ligeros Open Data (paradas ~330 KB + líneas ~22 KB), con búsqueda por código numérico (p. ej. `1580`) o nombre sin sensibilidad a tildes (p. ej. `acoran`, `guimar`, `nuevo obrero`).
  * Índice compacto `parada → líneas` (~97 KB) extraído automáticamente en segundo plano desde el GTFS oficial y guardado en `.storage` (refresco mensual) para mostrar únicamente las líneas que pasan por tu parada, con fallback automático a líneas en vivo + catálogo CSV.
* **Entidades nativas `timestamp`:**
  * Sensor agregado **`Próxima guagua`** por parada (`SensorDeviceClass.TIMESTAMP`) con cuenta atrás en vivo en el frontend de Home Assistant y atributo acotado `arrivals`.
  * Un **sensor individual por cada línea filtrada** (ideal para tarjetas *Tile* y automatizaciones sin plantillas). Si dejas el filtro vacío, solo se crea el sensor agregado.
  * Botón **`Actualizar`** por parada (`button.<parada>_actualizar`) con *cooldown* mínimo de 30 s para consultas puntuales (incluso fuera de horario).
* **Sondeo responsable con `titsa.com`:**
  * Intervalo seguro (90 s ± 10 s de *jitter*), semáforo global de concurrencia, `User-Agent` honesto, `always_update=False` y *backoff* exponencial ante `429`/`5xx`.
  * **Ventana horaria opcional:** vincula un Helper nativo (`schedule.*`, `binary_sensor.*` o `input_boolean.*`) para realizar **0 peticiones HTTP** fuera de tu franja horaria (las entidades pasan limpiamente a `unknown` con `polling_paused: true`, evitando falsos `unavailable` y cuentas atrás caducadas).
* **Preparada para GTFS-RT:** cliente Python desacoplado (`custom_components/titsa/api/`) basado en *Strategy Pattern* (`TitsaRealtimeProvider`) para migrar limpiamente cuando TITSA publique el feed GTFS-RT oficial sin romper tus entidades ni automatizaciones.

---

## Instalación

### Mediante HACS / Home Assistant Marketplace (recomendado)

[![Añadir repositorio a HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=alejandrojara&repository=ha-titsa&category=integration)

1. Pulsa el botón superior (o añade `https://github.com/alejandrojara/ha-titsa` como repositorio personalizado de tipo **Integración** en HACS).
2. Instala **TITSA** y reinicia Home Assistant.
3. Añade la integración desde **Ajustes → Dispositivos y servicios → Añadir integración → TITSA**, o directamente con este botón:

[![Añadir integración TITSA](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=titsa)

### Instalación manual

Copia la carpeta `custom_components/titsa` dentro de `<config>/custom_components/titsa` en tu servidor de Home Assistant y reinicia.

---

## Configuración

1. **Añadir la integración padre (`TITSA`):** solo se añade una vez y gestiona la caché local de Datos Abiertos y el límite de concurrencia.
2. **Añadir paradas:** dentro de la tarjeta de la integración **TITSA**, pulsa **Añadir parada**:
   * Introduce el **código numérico** de la parada (visible en la marquesina, p. ej. `1580`) o busca por **nombre** (p. ej. `Nuevo Obrero`).
   * Si varias paradas comparten nombre (sentido ida/vuelta), se mostrará el código y las líneas que pasan por cada una para elegir la correcta.
   * Opcionalmente selecciona las **líneas** que quieres seguir y vincula un Helper **`schedule.*`** (por ejemplo, `schedule.mananas_trabajo` de 07:00 a 08:30) para consultar solo cuando realmente lo necesites.
3. **Reconfigurar:** puedes cambiar en cualquier momento el nombre, el filtro de líneas o la ventana horaria desde el menú de la subentrada sin perder el historial del sensor agregado.

---

## Entidades creadas por parada

Cada parada crea su propio **Dispositivo** en Home Assistant con:

| Entidad | Tipo | Descripción |
| :--- | :--- | :--- |
| `sensor.<parada>_proxima_guagua` | `sensor` (`timestamp`) | Hora estimada de la próxima guagua (filtrada o de todas las líneas). Expone `arrivals`, `next_line`, `next_destination`, `next_minutes`, `polling_paused` y `last_estimation`. |
| `sensor.<parada>_linea_<N>` | `sensor` (`timestamp`) | Creado **solo para cada línea seleccionada en el filtro**. Expone `line`, `destination`, `minutes`, `following_arrivals` y `polling_paused`. |
| `button.<parada>_actualizar` | `button` | Refresco puntual bajo demanda (funciona también con la ventana horaria en pausa; *cooldown* de 30 s). |

> Los `entity_id` se generan automáticamente en el idioma de tu Home Assistant (en español `_proxima_guagua`, `_linea_<N>`, `_actualizar`; en inglés `_next_bus`, `_line_<N>`, `_refresh`).

### Estructura del atributo `arrivals`

```json
[
  {
    "line": "238",
    "line_name": "INTERCAMBIADOR SANTA CRUZ - SAN MATIAS - POR CHAMBERÍ",
    "destination": "INTERCAMBIADOR STA.CRUZ",
    "minutes": 0,
    "estimated_arrival": "2026-11-20T09:56:32+00:00",
    "is_approximate": false
  }
]
```

---

## Ejemplos de tarjetas nativas de Home Assistant

### 1. Tarjeta Markdown estilo marquesina (sin tarjetas personalizadas)

```yaml
type: markdown
title: 🚌 Nuevo Obrero (1580)
content: >
  {% set entity = 'sensor.nuevo_obrero_1580_proxima_guagua' %}
  {% set paused = state_attr(entity, 'polling_paused') %}
  {% set manual = state_attr(entity, 'manual_snapshot') %}
  {% set arrivals = state_attr(entity, 'arrivals') or [] %}
  {% if paused and not manual and arrivals | length == 0 %}
  ⏸️ **Sondeo en pausa** (fuera de ventana horaria). Pulsa *Actualizar* para consultar ahora.
  {% elif arrivals | length == 0 %}
  Sin llegadas estimadas en este momento.
  {% else %}
  | Línea | Destino | Llegada |
  | :---: | :--- | :---: |
  {% for bus in arrivals[:6] -%}
  | **{{ bus.line }}** | {{ bus.destination }} | {% if bus.minutes == 0 %}**Ahora**{% else %}{% if bus.is_approximate %}>{% endif %}{{ bus.minutes }} min{% endif %} |
  {% endfor %}
  {% endif %}
```

### 2. Tarjetas Tile nativas por línea + botón de refresco

```yaml
type: grid
columns: 2
square: false
cards:
  - type: tile
    entity: sensor.nuevo_obrero_1580_linea_238
    name: Línea 238
    icon: mdi:bus
    color: light-green
  - type: tile
    entity: sensor.nuevo_obrero_1580_linea_936
    name: Línea 936
    icon: mdi:bus
    color: green
  - type: tile
    entity: button.nuevo_obrero_1580_actualizar
    name: Actualizar ahora
    icon: mdi:refresh
    tap_action:
      action: perform-action
      perform_action: button.press
      target:
        entity_id: button.nuevo_obrero_1580_actualizar
```

---

## Aviso legal, atribución de datos y uso responsable

* **Componente no oficial y ausencia de afiliación:** `ha-titsa` es un software libre e independiente de la comunidad, **no oficial y sin soporte, respaldo ni vinculación comercial o técnica con Transportes Interurbanos de Tenerife, S.A. (TITSA) ni con el Cabildo Insular de Tenerife**. El nombre *TITSA* es una marca registrada de su respectivo titular y se emplea únicamente con fines descriptivos y de interoperabilidad para identificar el servicio de transporte consultado.
* **Exclusión de garantías:** Esta integración se proporciona *«tal cual»* (*as is*), sin garantía de disponibilidad continua ni exactitud de las estimaciones, y puede verse afectada por cambios no anunciados en los servicios web externos.
* **Catálogos de paradas, líneas y GTFS:** Datos Abiertos del **Cabildo Insular de Tenerife / TITSA** (`datos.tenerife.es`), publicados bajo licencia **[Creative Commons Attribution 4.0 (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/)**.
* **Tiempos de llegada en tiempo real:** consultados directamente desde tu instancia local de Home Assistant hacia `titsa.com` (sin intermediarios ni redistribución a terceros), con sondeo limitado (90 s ± 10 s, semáforo de concurrencia, *backoff* exponencial) y soporte nativo de ventanas horarias (`schedule.*`) para minimizar la carga sobre el servicio público.

---

## Licencia

Distribuido bajo licencia [MIT](LICENSE).
