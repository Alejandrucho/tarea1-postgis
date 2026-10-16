#!/usr/bin/env python3
"""
Prepara el extracto de OpenStreetMap del Laboratorio 05 (y de la Tarea 1).

    python3 preparar_datos.py                          # zona por defecto: gran-valparaiso
    python3 preparar_datos.py --zona santiago-centro   # la zona de la Tarea 1
    python3 preparar_datos.py --zona gran-concepcion
    python3 preparar_datos.py --bbox -71.75 -33.16 -71.33 -32.88 --nombre mi-zona
    python3 preparar_datos.py --forzar                 # rehace todo, sobrescribiendo
    python3 preparar_datos.py --sql                    # además deja un .sql (camino sin ogr2ogr)

Como en el Lab 03, los datos NO se generan: son reales y públicos. Vienen del
extracto de Chile que Geofabrik publica a diario a partir de OpenStreetMap
(licencia ODbL):

    Ficha:    https://download.geofabrik.de/south-america/chile.html
    Descarga: https://download.geofabrik.de/south-america/chile-latest.osm.pbf   (~330 MB)

El .osm.pbf descargado va en `datos_fuente/` (tal cual). Este script hace tres cosas:

  1. Recorta la zona pedida (una caja en lon/lat) y la convierte a un
     GeoPackage — `datos/osm_<zona>.gpkg` — con tres capas: `points`, `lines`
     y `multipolygons`, reproyectadas a SIRGAS-Chile / UTM 19S (SRID 5361), la
     proyección de trabajo del curso. Lo hace con `ogr2ogr`, el conversor
     universal de GDAL: si está instalado en la máquina se usa ese; si no, se
     construye la imagen `pmd-postgis-gdal` (el PostGIS de siempre + gdal-bin,
     ver `Dockerfile`) y se corre desde ahí.
  2. Verifica el resultado leyendo el GeoPackage directamente (es un SQLite):
     cuenta las filas de cada capa y comprueba el SRID. Sin dependencias de
     Python.
  3. Con `--sql`, genera además `datos/osm_<zona>.sql` (formato PGDump) para
     cargar con un simple `psql -f` en una máquina donde no se pueda usar
     ogr2ogr en clase. Es el camino de rescate, no el oficial.

El script NO descarga el .pbf por su cuenta: son ~330 MB y 25 equipos
bajándolos a las 08:30 saturan la red del laboratorio, igual que el ZIP de la
DPA en el Lab 03. Dejar el .pbf copiado en las máquinas antes de la clase — o
copiar directamente el `.gpkg` ya generado en una máquina (pesa mucho menos y
es lo único que la clase necesita). La construcción de la imagen sí necesita
red la primera vez (apt-get dentro de Docker): correrla también antes.
"""
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time

AQUI = os.path.dirname(os.path.abspath(__file__))
FUENTE = os.path.join(AQUI, "datos_fuente")
DESTINO = os.path.join(AQUI, "datos")

URL_FICHA = "https://download.geofabrik.de/south-america/chile.html"
URL_PBF = "https://download.geofabrik.de/south-america/chile-latest.osm.pbf"

SRID_CARGA = 5361
IMAGEN = "pmd-postgis-gdal"          # la construye `docker compose build` con el Dockerfile de esta carpeta
CAPAS = ("points", "lines", "multipolygons")

# Cajas en lon/lat (WGS 84): xmin ymin xmax ymax. Son cajas generosas, a propósito:
# el recorte se hace por caja y las comunas de verdad se cruzan después, en la base.
ZONAS = {
    "gran-valparaiso": (-71.75, -33.16, -71.33, -32.88),   # Valparaíso · Viña · Concón · Quilpué · Villa Alemana
    "santiago-centro": (-70.75, -33.52, -70.52, -33.36),   # Tarea 1: Santiago, Providencia, Ñuñoa, Macul, San Miguel…
    "gran-concepcion": (-73.22, -37.06, -72.92, -36.66),   # Concepción · Talcahuano · Hualpén · San Pedro · Chiguayante · Penco · Coronel
    "gran-santiago":   (-70.90, -33.68, -70.42, -33.28),   # la Región Metropolitana urbana completa: pesado, solo para quien quiera más
}


def salir(mensaje):
    print(f"\n⛔ {mensaje}", file=sys.stderr)
    sys.exit(1)


def argumentos():
    args = sys.argv[1:]
    zona, bbox, nombre = "gran-valparaiso", None, None
    forzar, sql = "--forzar" in args, "--sql" in args
    if "--zona" in args:
        zona = args[args.index("--zona") + 1]
        if zona not in ZONAS:
            salir(f"Zona desconocida: {zona}. Disponibles: {', '.join(ZONAS)}")
    if "--bbox" in args:
        i = args.index("--bbox")
        try:
            bbox = tuple(float(v) for v in args[i + 1:i + 5])
            assert len(bbox) == 4 and bbox[0] < bbox[2] and bbox[1] < bbox[3]
        except (ValueError, AssertionError):
            salir("--bbox espera cuatro números: xmin ymin xmax ymax (lon/lat, oeste y sur negativos)")
        if "--nombre" not in args:
            salir("--bbox necesita --nombre <nombre-de-la-zona> para nombrar el archivo de salida")
        nombre = args[args.index("--nombre") + 1]
    if bbox is None:
        bbox, nombre = ZONAS[zona], zona
    if not re.fullmatch(r"[a-z0-9][a-z0-9\-]*", nombre):
        salir("El nombre de la zona va en minúsculas, sin espacios ni tildes (ej. gran-valparaiso)")
    return nombre, bbox, forzar, sql


# ------------------------------------------------------------------ fuente
def elegir_pbf():
    os.makedirs(FUENTE, exist_ok=True)
    pbfs = sorted(f for f in os.listdir(FUENTE) if f.lower().endswith(".osm.pbf"))
    if not pbfs:
        salir(
            "No hay ningún .osm.pbf en datos_fuente/.\n\n"
            f"   1. Abre la ficha del extracto:  {URL_FICHA}\n"
            f"   2. Descarga «chile-latest.osm.pbf» (~330 MB):\n"
            f"      {URL_PBF}\n"
            f"   3. Déjalo, tal cual, en:\n"
            f"      {FUENTE}\n\n"
            "   Y vuelve a ejecutar este script."
        )
    if len(pbfs) > 1:
        print(f"⚠️  Hay {len(pbfs)} archivos .osm.pbf en datos_fuente/; se usa el primero: {pbfs[0]}")
    ruta = os.path.join(FUENTE, pbfs[0])
    mb = os.path.getsize(ruta) / 1e6
    print(f"Fuente: {pbfs[0]} ({mb:.0f} MB)")
    if mb < 50:
        print("   (es chico para ser Chile completo: ¿es un recorte ya hecho? Se usa igual.)")
    return ruta


# ------------------------------------------------------------------ ogr2ogr
class Ogr:
    """Ejecuta ogr2ogr/ogrinfo, localmente o dentro de la imagen del curso."""

    def __init__(self):
        self.local = shutil.which("ogr2ogr")
        if self.local:
            v = subprocess.run(["ogr2ogr", "--version"], capture_output=True, text=True).stdout.strip()
            print(f"ogr2ogr local: {v}")
        else:
            print("ogr2ogr no está en esta máquina: se usa la imagen Docker del curso.")
            self._asegurar_imagen()

    def _asegurar_imagen(self):
        if subprocess.run(["docker", "image", "inspect", IMAGEN], capture_output=True).returncode == 0:
            print(f"   imagen {IMAGEN} ya construida")
            return
        print(f"   construyendo {IMAGEN} (PostGIS + gdal-bin; necesita red una vez)...")
        r = subprocess.run(["docker", "compose", "build"], cwd=AQUI)
        if r.returncode != 0:
            salir("No se pudo construir la imagen. ¿Docker corriendo? ¿Hay red para el apt-get?\n"
                  "   Alternativa: instalar GDAL en la máquina (brew install gdal / apt install gdal-bin /\n"
                  "   conda install -c conda-forge gdal) y volver a correr este script.")

    def run(self, herramienta, args, montajes):
        """`montajes` es {ruta_local_dir: ruta_en_contenedor}; los args ya vienen con rutas de contenedor."""
        if self.local:
            # Deshacer el mapeo: en local se usan las rutas reales.
            inverso = {v: k for k, v in montajes.items()}
            args = [self._local(a, inverso) for a in args]
            return subprocess.run([herramienta] + args, capture_output=True, text=True)
        cmd = ["docker", "run", "--rm"]
        for local, interno in montajes.items():
            cmd += ["-v", f"{local}:{interno}"]
        cmd += [IMAGEN, herramienta] + args
        return subprocess.run(cmd, capture_output=True, text=True)

    @staticmethod
    def _local(arg, inverso):
        for interno, local in inverso.items():
            if arg.startswith(interno + "/"):
                return local + arg[len(interno):]
        return arg


def recortar(ogr, pbf, nombre, bbox, forzar):
    """OSM (.pbf) → GeoPackage con la caja pedida, en SRID 5361."""
    os.makedirs(DESTINO, exist_ok=True)
    salida = os.path.join(DESTINO, f"osm_{nombre}.gpkg")
    if os.path.exists(salida) and not forzar:
        print(f"\n{os.path.basename(salida)} ya existe ({os.path.getsize(salida) / 1e6:.0f} MB), "
              "se conserva (usa --forzar para rehacerlo)")
        return salida
    if os.path.exists(salida):
        os.remove(salida)
    xmin, ymin, xmax, ymax = bbox
    print(f"\nRecortando la caja lon [{xmin}, {xmax}] · lat [{ymin}, {ymax}] y reproyectando a {SRID_CARGA}...")
    print("   (lee el archivo completo una vez; en Chile entero son unos minutos — hay una barra de progreso)")
    args = [
        "-f", "GPKG", f"/salida/osm_{nombre}.gpkg",
        f"/fuente/{os.path.basename(pbf)}",
        *CAPAS,                                   # solo estas tres capas; multilinestrings y other_relations no se usan
        "-spat", str(xmin), str(ymin), str(xmax), str(ymax),   # la caja, en el SRID de origen (4326)
        "-t_srs", f"EPSG:{SRID_CARGA}",           # reproyectar al cargar, como el -s 5360:5361 de shp2pgsql
        "-progress",
        "--config", "OSM_MAX_TMPFILE_SIZE", "4000",   # caché de nodos en memoria antes de irse a disco
    ]
    t0 = time.perf_counter()
    r = ogr.run("ogr2ogr", args, {os.path.dirname(pbf): "/fuente", DESTINO: "/salida"})
    if r.returncode != 0 or not os.path.exists(salida):
        salir("ogr2ogr falló.\n   stderr: " + r.stderr.strip()[-600:])
    avisos = len([l for l in r.stderr.splitlines() if "Warning" in l])
    print(f"   → {os.path.basename(salida)} ({os.path.getsize(salida) / 1e6:.0f} MB) en {time.perf_counter() - t0:.0f} s"
          + (f" · {avisos} advertencias de geometría (normal en OSM: anillos abiertos, polígonos raros)" if avisos else ""))
    return salida


# ------------------------------------------------------------------ verificar
def verificar(gpkg, nombre):
    """Un GeoPackage es un SQLite: se interroga sin GDAL."""
    con = sqlite3.connect(gpkg)
    capas = dict(con.execute("SELECT table_name, srs_id FROM gpkg_contents WHERE data_type = 'features'").fetchall())
    faltan = [c for c in CAPAS if c not in capas]
    if faltan:
        salir(f"Al GeoPackage le faltan capas: {faltan}. Capas presentes: {list(capas)}")
    srids = set(capas[c] for c in CAPAS)
    assert srids == {SRID_CARGA}, f"las capas no están en {SRID_CARGA}: {capas}"
    conteos = {c: con.execute(f'SELECT count(*) FROM "{c}"').fetchone()[0] for c in CAPAS}
    con.close()
    total = sum(conteos.values())
    if total < 1000:
        salir(f"El recorte trae solo {total} objetos: la caja no cae donde debe o el .pbf no es el de Chile.")
    print(f"\nVerificación OK: {conteos['points']:,} puntos · {conteos['lines']:,} líneas · "
          f"{conteos['multipolygons']:,} polígonos · SRID {SRID_CARGA} → osm_{nombre}.gpkg "
          f"({os.path.getsize(gpkg) / 1e6:.0f} MB)".replace(",", "."))
    return conteos


# ------------------------------------------------------------------ SQL (camino de rescate)
NOMBRES_TABLAS = {"points": "osm_puntos", "lines": "osm_lineas", "multipolygons": "osm_poligonos"}


def generar_sql(ogr, gpkg, nombre, forzar):
    """GeoPackage → un .sql en formato PGDump, para cargar con `psql -f` sin ogr2ogr."""
    salida = os.path.join(DESTINO, f"osm_{nombre}.sql")
    if os.path.exists(salida) and not forzar:
        print(f"\n{os.path.basename(salida)} ya existe ({os.path.getsize(salida) / 1e6:.0f} MB), se conserva")
        return
    print(f"\nGenerando {os.path.basename(salida)} (PGDump)...")
    partes = []
    for capa, tabla in NOMBRES_TABLAS.items():
        parcial = os.path.join(DESTINO, f"_{tabla}.sql")
        args = ["-f", "PGDump", f"/salida/_{tabla}.sql", f"/salida/osm_{nombre}.gpkg", capa,
                "-nln", tabla, "-lco", "GEOMETRY_NAME=geom", "-lco", "SPATIAL_INDEX=GIST",
                "-lco", "COLUMN_TYPES=other_tags=hstore", "-lco", "FID=id"]
        r = ogr.run("ogr2ogr", args, {DESTINO: "/salida"})
        if r.returncode != 0 or not os.path.exists(parcial):
            salir(f"ogr2ogr (PGDump, {capa}) falló.\n   stderr: " + r.stderr.strip()[-400:])
        partes.append(parcial)
    with open(salida, "w", encoding="utf-8") as f:
        f.write("CREATE EXTENSION IF NOT EXISTS postgis;\nCREATE EXTENSION IF NOT EXISTS hstore;\n")
        for p in partes:
            with open(p, encoding="utf-8") as src:
                shutil.copyfileobj(src, f)
            os.remove(p)
    print(f"   → {os.path.basename(salida)} ({os.path.getsize(salida) / 1e6:.0f} MB) — "
          f"carga: docker compose exec -T db psql -q -U postgres -f /datos/osm_{nombre}.sql")


if __name__ == "__main__":
    nombre, bbox, forzar, sql = argumentos()
    print(f"Preparando los datos del Laboratorio 05 — zona «{nombre}»...")
    pbf = elegir_pbf()
    ogr = Ogr()
    gpkg = recortar(ogr, pbf, nombre, bbox, forzar)
    verificar(gpkg, nombre)
    if sql:
        generar_sql(ogr, gpkg, nombre, forzar)
