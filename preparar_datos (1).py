#!/usr/bin/env python3
"""
Prepara el shapefile de comunas del Laboratorio 03 a partir del archivo oficial.

    python3 preparar_datos.py             # prepara lo que falte
    python3 preparar_datos.py --forzar    # rehace todo, sobrescribiendo

A diferencia de los laboratorios 01 y 02, aquí los datos NO se generan: son
reales y públicos. Vienen de la División Política Administrativa 2023 de IDE
Chile, publicada en datos.gob.cl con licencia CC-BY.

    Ficha:    https://datos.gob.cl/dataset/categoria-geoespacial-limites-y-fronteras
    Descarga: https://www.geoportal.cl/geoportal/catalog/download/912598ad-ac92-35f6-8045-098f214bd9c2

El ZIP descargado va en `datos_fuente/` (tal cual, sin descomprimir). Este
script hace tres cosas:

  1. Extrae el shapefile de comunas a `datos/COMUNAS.*`.
  2. Verifica lo que el notebook promete: 345 comunas en el país (la Antártica
     no viene en la DPA) y 38 en la Región de Valparaíso, en SRID 5360.
  3. Genera `datos/comunas_cl.sql` con `shp2pgsql` — la imagen del laboratorio
     no trae esa herramienta, así que se corre desde la imagen
     `postgis/postgis:16-3.4-alpine` (se descarga sola la primera vez). El SQL
     carga las comunas reproyectadas a SIRGAS-Chile / UTM 19S (SRID 5361).

El script NO descarga el ZIP por su cuenta: son ~311 MB y 25 equipos bajándolos
a las 08:30 saturan la red del laboratorio, igual que la imagen de Docker del
Lab 02. Dejar el ZIP copiado en las máquinas antes de la clase. El paso 3 sí
necesita red la primera vez (imagen alpine + un paquete de ~1 MB): correrlo
también antes de la clase.

Sin dependencias de Python: lee el .shp y el .dbf con la biblioteca estándar.
Docker sí hace falta para el paso 3 — pero sin Docker no hay laboratorio.
"""
import os
import shutil
import struct
import subprocess
import sys
import zipfile

AQUI = os.path.dirname(os.path.abspath(__file__))
FUENTE = os.path.join(AQUI, "datos_fuente")
DESTINO = os.path.join(AQUI, "datos")
FORZAR = "--forzar" in sys.argv

URL_FICHA = "https://datos.gob.cl/dataset/categoria-geoespacial-limites-y-fronteras"
URL_ZIP = ("https://www.geoportal.cl/geoportal/catalog/download/"
           "912598ad-ac92-35f6-8045-098f214bd9c2")

# Extensiones que acompañan a un shapefile. .shp/.shx/.dbf son obligatorias;
# .prj es la que dice en qué sistema de coordenadas están las coordenadas —
# sin ella no se sabe dónde queda nada.
EXTENSIONES = (".shp", ".shx", ".dbf", ".prj", ".cpg", ".sbn", ".sbx", ".qmd", ".xml")

# Chile tiene 346 comunas, pero la DPA no cubre el Territorio Antártico:
# la comuna Antártica no viene. Es material de discusión del Ejercicio 2.3.
COMUNAS_PAIS = 345
COMUNAS_REGION_05 = 38

# La DPA 2023 viene en SIRGAS-Chile geográfico (grados). El SQL de carga la
# reproyecta a la proyección de trabajo del curso: SIRGAS-Chile / UTM 19S.
SRID_CARGA = 5361
IMAGEN_HERRAMIENTAS = "postgis/postgis:16-3.4-alpine"


def salir(mensaje):
    print(f"\n⛔ {mensaje}", file=sys.stderr)
    sys.exit(1)


# ------------------------------------------------------------------ lectores
def contar_registros_dbf(ruta):
    """Número de registros declarado en la cabecera del .dbf (bytes 4-7)."""
    with open(ruta, "rb") as f:
        f.seek(4)
        return struct.unpack("<I", f.read(4))[0]


def encoding_desde_cpg(ruta, defecto="latin-1"):
    """Codificación del .dbf según el .cpg (la DPA 2023 dice UTF-8)."""
    try:
        texto = open(ruta, encoding="ascii", errors="ignore").read().strip().upper()
    except OSError:
        return defecto
    if "UTF" in texto:
        return "utf-8"
    if "8859" in texto or "LATIN" in texto:
        return "latin-1"
    return defecto


def leer_dbf(ruta, encoding="latin-1"):
    """Lee un .dbf completo y devuelve (campos, filas como dicts).

    Formato dBase III: cabecera de 32 bytes, luego un descriptor de 32 bytes
    por campo, terminado en 0x0D, y después los registros de largo fijo.
    """
    with open(ruta, "rb") as f:
        cabecera = f.read(32)
        n_registros, inicio_datos, largo_registro = struct.unpack("<I H H", cabecera[4:12])

        campos = []
        while True:
            desc = f.read(32)
            if desc[0:1] in (b"\r", b"", b"\x1a"):
                break
            nombre = desc[:11].split(b"\x00")[0].decode(encoding).strip()
            campos.append((nombre, desc[16]))  # (nombre, largo en bytes)

        f.seek(inicio_datos)
        filas = []
        for _ in range(n_registros):
            crudo = f.read(largo_registro)
            if not crudo or crudo[:1] == b"\x1a":
                break
            if crudo[:1] == b"*":  # registro marcado como borrado
                continue
            pos, fila = 1, {}
            for nombre, largo in campos:
                fila[nombre] = crudo[pos:pos + largo].decode(encoding, "replace").strip()
                pos += largo
            filas.append(fila)
    return [c[0] for c in campos], filas


def srid_desde_prj(ruta):
    """Adivina el SRID leyendo el .prj. Devuelve (srid, texto_del_prj)."""
    if not os.path.exists(ruta):
        return None, None
    texto = open(ruta, encoding="latin-1").read().strip()
    plano = texto.upper().replace("_", " ").replace("-", " ")
    if "UTM" in plano and "19S" in plano.replace(" ", ""):
        return (5361 if "SIRGAS" in plano else 32719), texto
    if "UTM" in plano and "18S" in plano.replace(" ", ""):
        return (5362 if "SIRGAS" in plano else 32718), texto
    if "GEOGCS" in plano and "PROJCS" not in plano and "SIRGAS" in plano:
        return 5360, texto  # SIRGAS-Chile geográfico (grados) — el de la DPA 2023
    if "WGS 84" in plano and "PROJCS" not in plano.replace(" ", ""):
        return 4326, texto
    return None, texto


# ------------------------------------------------------------------ extraer
def elegir_shapefile(nombres):
    """Elige, entre los .shp del ZIP, el de comunas."""
    shps = [n for n in nombres if n.lower().endswith(".shp")]
    if not shps:
        salir(f"El ZIP no contiene ningún .shp.\n   Archivos: {nombres[:20]}")
    comunas = [n for n in shps if "comuna" in os.path.basename(n).lower()]
    if len(comunas) == 1:
        return comunas[0]
    if len(comunas) > 1:
        salir("Hay más de un shapefile de comunas en el ZIP; revisar a mano:\n   "
              + "\n   ".join(comunas))
    salir("Ninguno de los .shp del ZIP parece ser el de comunas:\n   "
          + "\n   ".join(shps))


def extraer():
    if not os.path.isdir(FUENTE):
        os.makedirs(FUENTE, exist_ok=True)
    zips = [f for f in sorted(os.listdir(FUENTE)) if f.lower().endswith(".zip")]
    if not zips:
        salir(
            "No hay ningún .zip en datos_fuente/.\n\n"
            f"   1. Abre la ficha del dataset:  {URL_FICHA}\n"
            f"   2. Descarga «División Política Administrativa 2023» (~311 MB):\n"
            f"      {URL_ZIP}\n"
            f"   3. Deja el ZIP, sin descomprimir, en:\n"
            f"      {FUENTE}\n\n"
            "   Y vuelve a ejecutar este script."
        )
    if len(zips) > 1:
        print(f"⚠️  Hay {len(zips)} archivos .zip en datos_fuente/; se usa el primero: {zips[0]}")

    ruta_zip = os.path.join(FUENTE, zips[0])
    os.makedirs(DESTINO, exist_ok=True)

    with zipfile.ZipFile(ruta_zip) as z:
        nombres = z.namelist()
        shp = elegir_shapefile(nombres)
        base = shp[:-4]
        print(f"Shapefile de comunas dentro del ZIP: {shp}")

        copiados = []
        for nombre in nombres:
            raiz, ext = os.path.splitext(nombre)
            if raiz == base and ext.lower() in EXTENSIONES:
                salida = os.path.join(DESTINO, "COMUNAS" + ext.lower())
                if os.path.exists(salida) and not FORZAR:
                    print(f"  {os.path.basename(salida)} ya existe, se conserva "
                          "(usa --forzar para rehacerlo)")
                    copiados.append(salida)
                    continue
                with z.open(nombre) as origen, open(salida, "wb") as destino:
                    shutil.copyfileobj(origen, destino)
                copiados.append(salida)
                print(f"  → {os.path.basename(salida)} "
                      f"({os.path.getsize(salida) / 1e6:.1f} MB)")

    faltan = [e for e in (".shp", ".shx", ".dbf")
              if not os.path.exists(os.path.join(DESTINO, "COMUNAS" + e))]
    if faltan:
        salir(f"Al shapefile extraído le faltan archivos obligatorios: {faltan}")
    return copiados


# ------------------------------------------------------------------ verificar
def columna_region(campos):
    """Encuentra la columna que trae el código de región."""
    candidatas = [c for c in campos
                  if "REGION" in c.upper() and any(x in c.upper()
                                                   for x in ("COD", "CUT", "CODIGO"))]
    if not candidatas:
        candidatas = [c for c in campos if c.upper() in ("CODREGION", "REGION", "CUT_REG")]
    return candidatas[0] if candidatas else None


def columna_nombre(campos):
    """Encuentra la columna con el nombre de la comuna."""
    for c in campos:
        if c.upper() in ("COMUNA", "NOM_COMUNA", "NOMBRE", "NOM_COM"):
            return c
    candidatas = [c for c in campos if "COMUNA" in c.upper() and "COD" not in c.upper()
                  and "CUT" not in c.upper()]
    return candidatas[0] if candidatas else None


def verificar():
    prj = os.path.join(DESTINO, "COMUNAS.prj")
    srid, texto = srid_desde_prj(prj)
    if texto is None:
        salir("El shapefile NO trae .prj: sin él no se sabe el SRID y no se puede cargar.")
    print(f"\n.prj → {texto[:110]}{'…' if len(texto) > 110 else ''}")
    if srid:
        print(f"SRID de origen detectado: {srid} (el SQL de carga reproyecta a {SRID_CARGA})")
    else:
        salir("No se pudo deducir el SRID del .prj: revisarlo a mano y ajustar este script.")

    encoding = encoding_desde_cpg(os.path.join(DESTINO, "COMUNAS.cpg"))
    print(f".cpg → codificación del .dbf: {encoding}")

    n_shp = contar_registros_dbf(os.path.join(DESTINO, "COMUNAS.dbf"))
    campos, filas = leer_dbf(os.path.join(DESTINO, "COMUNAS.dbf"), encoding)
    print(f"\nColumnas del .dbf ({len(campos)}): {', '.join(campos)}")

    assert n_shp == len(filas), f"cabecera dice {n_shp} registros, se leyeron {len(filas)}"
    assert len(filas) == COMUNAS_PAIS, \
        f"se esperaban {COMUNAS_PAIS} comunas y hay {len(filas)} — ¿cambió la versión del dataset?"

    col_reg = columna_region(campos)
    col_nom = columna_nombre(campos)
    if col_reg is None or col_nom is None:
        salir("No se reconocieron las columnas de región/comuna en el .dbf.\n"
              f"   Columnas disponibles: {campos}\n"
              "   Ajustar `columna_region`/`columna_nombre` en este script.")

    def es_region_05(v):
        v = v.strip().lstrip("0")
        return v in ("5", "05")

    region_05 = [f for f in filas if es_region_05(f[col_reg])]
    assert len(region_05) == COMUNAS_REGION_05, (
        f"se esperaban {COMUNAS_REGION_05} comunas en la Región de Valparaíso "
        f"(columna {col_reg}) y hay {len(region_05)}")

    nombres = sorted(f[col_nom] for f in region_05)
    for esperada in ("Valparaíso", "Viña del Mar", "Concón", "Quilpué", "Villa Alemana", "Limache"):
        assert any(esperada.lower() == n.lower() for n in nombres), \
            f"falta la comuna «{esperada}» en la Región de Valparaíso — ¿problema de codificación?"

    print(f"\nColumna de región: {col_reg} · columna de nombre: {col_nom}")
    print(f"Comunas de la Región de Valparaíso ({len(nombres)}): {', '.join(nombres)}")
    print(f"\nVerificación OK: {len(filas)} comunas del país (la Antártica no viene en la DPA) · "
          f"{len(region_05)} en la Región de Valparaíso · SRID {srid} → carga en {SRID_CARGA}")
    return srid, encoding


# ------------------------------------------------------------------ SQL de carga
def generar_sql(srid_origen, encoding):
    """Corre shp2pgsql (desde la imagen alpine) y deja datos/comunas_cl.sql.

    La imagen del laboratorio (postgis/postgis:16-3.4, Debian) trae el motor
    pero NO las utilidades de cliente; la variante alpine sí trae shp2pgsql
    (le falta una biblioteca, gettext-libs, que se instala al vuelo: ~1 MB).
    Así la clase carga con un simple `psql -f`, sin red ni herramientas extra.
    """
    salida = os.path.join(DESTINO, "comunas_cl.sql")
    if os.path.exists(salida) and not FORZAR:
        print(f"\ncomunas_cl.sql ya existe ({os.path.getsize(salida) / 1e6:.0f} MB), "
              "se conserva (usa --forzar para rehacerlo)")
        return
    print(f"\nGenerando comunas_cl.sql con shp2pgsql ({IMAGEN_HERRAMIENTAS})...")
    w = "UTF-8" if encoding == "utf-8" else "LATIN1"
    comando = (f"apk add -q gettext-libs && "
               f"shp2pgsql -D -s {srid_origen}:{SRID_CARGA} -I -W {w} "
               f"/d/COMUNAS public.comunas_cl > /d/comunas_cl.sql")
    r = subprocess.run(["docker", "run", "--rm", "-v", f"{DESTINO}:/d",
                        IMAGEN_HERRAMIENTAS, "sh", "-c", comando],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(salida) or os.path.getsize(salida) < 1e6:
        salir("shp2pgsql falló. ¿Docker corriendo? ¿Hay red para bajar la imagen "
              f"alpine y gettext-libs?\n   stderr: {r.stderr.strip()[-400:]}")
    print(f"  → comunas_cl.sql ({os.path.getsize(salida) / 1e6:.0f} MB) — "
          f"reproyectado de {srid_origen} (grados) a {SRID_CARGA} (UTM 19S, metros)")


if __name__ == "__main__":
    print("Preparando los datos del Laboratorio 03...")
    extraer()
    srid_origen, encoding = verificar()
    generar_sql(srid_origen, encoding)
