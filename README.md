# aparcaqui-datos

Histórico abierto de la **ocupación de los aparcamientos públicos de Sevilla** y de las
**retenciones de tráfico**. Los datos se leen cada 5 minutos de los datos que publica el
Ayuntamiento en [trafico.sevilla.org](https://trafico.sevilla.org/). Forma parte del proyecto Aparcaquí.

## Cómo funciona

- `recolector.py` (solo biblioteca estándar de Python) descarga `aparcamientos.xml` e
  `itrafico.xml`, corrige la codificación y añade las lecturas a CSV comprimidos por hora.
- `.github/workflows/recolectar.yml` lo ejecuta cada hora en GitHub Actions (12 lecturas,
  una cada 5 minutos) y hace un único commit por hora.

## Estructura

```
datos/parkings/AAAA/MM/DD/HH.csv.gz     ts_utc, codigo, nombre, capacidad, libres, ocupacion, estado, fecha_fuente, lat, lon
datos/incidencias/AAAA/MM/DD/HH.csv.gz  ts_utc, codigo, descripcion, n_puntos, longitud_m, lat, lon, polilinea
```

- `ts_utc`: momento de la lectura (UTC).
- `fecha_fuente`: última actualización del parking según el Ayuntamiento (hora de Madrid).
  Si no cambia durante mucho tiempo, o si `estado` es `SIN INFORMACION`, el dato está obsoleto.
- La hora del nombre del fichero es la hora local de Madrid.

Para leerlo con pandas: `pd.read_csv("datos/parkings/2026/09/19/14.csv.gz")`.

## Fuente y licencia

Datos publicados por el Ayuntamiento de Sevilla (Centro de Gestión de la Movilidad) y reutilizados
al amparo de la Ley 37/2007 sobre reutilización de la información del sector público. Cita
la fuente si los usas.
