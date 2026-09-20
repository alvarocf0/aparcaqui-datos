"""Recolector de datos abiertos de tráfico de Sevilla (trafico.sevilla.org).

Guarda cada lectura en CSV comprimidos por hora:
    <salida>/parkings/AAAA/MM/DD/HH.csv.gz
    <salida>/incidencias/AAAA/MM/DD/HH.csv.gz

Solo usa la biblioteca estándar de Python, para que GitHub Actions lo ejecute sin
instalar nada. El backend de Aparcaquí reutiliza estas mismas funciones de parseo.

Uso:
    python recolector.py                                  # una lectura
    python recolector.py --iteraciones 12 --intervalo 300 # una hora, cada 5 min
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

URL_PARKINGS = "https://trafico.sevilla.org/datos/aparcamientos.xml"
URL_INCIDENCIAS = "https://trafico.sevilla.org/datos/itrafico.xml"
USER_AGENT = "Aparcaqui-recolector/0.1 (datos abiertos; lectura cada 5 min)"
TZ = ZoneInfo("Europe/Madrid")

CAMPOS_PARKINGS = [
    "ts_utc", "codigo", "nombre", "capacidad", "libres", "ocupacion",
    "estado", "fecha_fuente", "lat", "lon",
]
CAMPOS_INCIDENCIAS = [
    "ts_utc", "codigo", "descripcion", "n_puntos", "longitud_m", "lat", "lon", "polilinea",
]


# --------------------------------------------------------------------------- descarga

def descargar(url: str, intentos: int = 3, timeout: int = 20) -> bytes:
    """Descarga una URL con reintentos y un User-Agent identificable."""
    ultimo_error: Exception | None = None
    for intento in range(intentos):
        try:
            peticion = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(peticion, timeout=timeout) as respuesta:
                return respuesta.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            ultimo_error = error
            time.sleep(2 * (intento + 1))
    raise RuntimeError(f"No se pudo descargar {url}: {ultimo_error}")


def decodificar_xml(contenido: bytes) -> str:
    """Devuelve el XML como texto, corrigiendo la codificación del feed.

    El feed trae bytes UTF-8 (a veces con BOM) aunque itrafico.xml declara
    iso-8859-1; si se confiara en la declaración, los acentos saldrían rotos.
    """
    if contenido.startswith(b"\xef\xbb\xbf"):
        contenido = contenido[3:]
    try:
        texto = contenido.decode("utf-8")
    except UnicodeDecodeError:
        texto = contenido.decode("latin-1")
    # Quitamos la declaración para que el parser no intente recodificar.
    return re.sub(r"^\s*<\?xml[^>]*\?>", "", texto)


# --------------------------------------------------------------------------- parseo

def _fecha_iso(texto: str | None) -> str:
    """'19/09/2026 14:22:55' o '19-09-2026 14:24:06' (hora local) -> ISO con zona."""
    if not texto:
        return ""
    texto = texto.strip().replace("-", "/")
    for formato in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(texto, formato).replace(tzinfo=TZ).isoformat()
        except ValueError:
            continue
    return ""


def _entero(valor: str | None) -> int | None:
    try:
        return int(float(valor)) if valor not in (None, "") else None
    except ValueError:
        return None


def parsear_parkings(xml: str) -> tuple[str, list[dict]]:
    """Parsea aparcamientos.xml. Devuelve (fecha del feed ISO, lista de parkings)."""
    raiz = ET.fromstring(xml)
    info = raiz.find("info")
    fecha_feed = _fecha_iso(info.get("fechahora") if info is not None else None)
    parkings = []
    for nodo in raiz.iter("aparcamiento"):
        try:
            lat_txt, lon_txt = (nodo.get("coordenadas") or "").split(",")
            lat, lon = float(lat_txt), float(lon_txt)
        except ValueError:
            continue
        parkings.append({
            "codigo": _entero(nodo.get("codigo")),
            "nombre": (nodo.get("nombre") or "").strip(),
            "capacidad": _entero(nodo.get("capacidad")),
            "libres": _entero(nodo.get("plazaslibres")),
            "ocupacion": _entero(nodo.get("ocupacion")),
            "estado": (nodo.get("estado") or "").strip(),
            "fecha_fuente": _fecha_iso(nodo.get("fechayhora")),
            "lat": lat,
            "lon": lon,
        })
    return fecha_feed, parkings


def _distancia_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import asin, cos, radians, sin, sqrt
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * 6_371_000 * asin(sqrt(a))


def parsear_incidencias(xml: str) -> tuple[str, list[dict]]:
    """Parsea itrafico.xml (retenciones). La polilínea viene como 'lon,lat lon,lat ...'."""
    raiz = ET.fromstring(xml)
    info = raiz.find("info")
    fecha_feed = _fecha_iso(info.get("fechahora") if info is not None else None)
    incidencias = []
    for nodo in raiz.iter("incidencia"):
        puntos = []
        for par in (nodo.get("polilinea") or "").split():
            try:
                lon_txt, lat_txt = par.split(",")
                puntos.append((float(lat_txt), float(lon_txt)))
            except ValueError:
                continue
        if not puntos:
            continue
        longitud = sum(_distancia_m(*puntos[i], *puntos[i + 1]) for i in range(len(puntos) - 1))
        incidencias.append({
            "codigo": _entero(nodo.get("codigo")),
            "descripcion": (nodo.get("descripcion") or "").strip(),
            "n_puntos": len(puntos),
            "longitud_m": round(longitud),
            "lat": round(sum(p[0] for p in puntos) / len(puntos), 6),
            "lon": round(sum(p[1] for p in puntos) / len(puntos), 6),
            "polilinea": " ".join(f"{lat:.6f},{lon:.6f}" for lat, lon in puntos),
            "puntos": puntos,
        })
    return fecha_feed, incidencias


# --------------------------------------------------------------------------- escritura

def ruta_horaria(salida: Path, tipo: str, momento_utc: datetime) -> Path:
    local = momento_utc.astimezone(TZ)
    return salida / tipo / f"{local:%Y}" / f"{local:%m}" / f"{local:%d}" / f"{local:%H}.csv.gz"


def anexar_csv_gz(ruta: Path, campos: list[str], filas: list[dict]) -> None:
    """Añade filas a un CSV comprimido (cada llamada es un miembro gzip; se lee como uno solo)."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    nuevo = not ruta.exists()
    buffer = io.StringIO()
    escritor = csv.DictWriter(buffer, fieldnames=campos, extrasaction="ignore", lineterminator="\n")
    if nuevo:
        escritor.writeheader()
    escritor.writerows(filas)
    with gzip.open(ruta, "at", encoding="utf-8", newline="") as fichero:
        fichero.write(buffer.getvalue())


def recolectar_una_vez(salida: Path) -> dict:
    """Descarga ambos feeds y los guarda. Devuelve un resumen para el log."""
    ahora = datetime.now(timezone.utc).replace(microsecond=0)
    resumen = {"ts_utc": ahora.isoformat(), "parkings": None, "incidencias": None, "errores": []}

    try:
        _, parkings = parsear_parkings(decodificar_xml(descargar(URL_PARKINGS)))
        filas = [{**p, "ts_utc": ahora.isoformat()} for p in parkings]
        anexar_csv_gz(ruta_horaria(salida, "parkings", ahora), CAMPOS_PARKINGS, filas)
        resumen["parkings"] = len(filas)
    except Exception as error:  # noqa: BLE001 - queremos seguir con el otro feed
        resumen["errores"].append(f"parkings: {error}")

    try:
        _, incidencias = parsear_incidencias(decodificar_xml(descargar(URL_INCIDENCIAS)))
        filas = [{**i, "ts_utc": ahora.isoformat()} for i in incidencias]
        if filas:
            anexar_csv_gz(ruta_horaria(salida, "incidencias", ahora), CAMPOS_INCIDENCIAS, filas)
        resumen["incidencias"] = len(filas)
    except Exception as error:  # noqa: BLE001
        resumen["errores"].append(f"incidencias: {error}")

    return resumen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--salida", default="datos", help="carpeta raíz de salida (por defecto: datos)")
    parser.add_argument("--iteraciones", type=int, default=1, help="número de lecturas")
    parser.add_argument("--intervalo", type=int, default=300, help="segundos entre lecturas")
    parser.add_argument("--duracion-max", type=int, default=0,
                        help="segundos máximos de ejecución (0 = sin límite); útil en GitHub Actions")
    args = parser.parse_args()

    salida = Path(args.salida)
    inicio = time.monotonic()
    lecturas_ok = 0
    for i in range(args.iteraciones):
        resumen = recolectar_una_vez(salida)
        if resumen["parkings"]:
            lecturas_ok += 1
        print(f"[{resumen['ts_utc']}] parkings={resumen['parkings']} "
              f"incidencias={resumen['incidencias']} errores={resumen['errores'] or '-'}", flush=True)
        if i == args.iteraciones - 1:
            break
        if args.duracion_max and time.monotonic() - inicio + args.intervalo > args.duracion_max:
            break
        time.sleep(args.intervalo)

    # Fallo visible (GitHub avisa por email) solo si no se pudo leer nada.
    return 0 if lecturas_ok else 1


if __name__ == "__main__":
    sys.exit(main())
