# PostgreSQL 16 (Debian bookworm) con PostGIS, más las herramientas de línea de
# comandos de GDAL (ogr2ogr, ogrinfo) y las de PostGIS (shp2pgsql, pgsql2shp).
#
# Por qué: instalar GDAL y PostGIS aquí evita instalarlos en cada máquina del
# laboratorio; todo corre dentro del contenedor.
#
# Se construye una vez, antes de la clase (necesita red para el apt-get):
#     docker compose build
FROM postgres:16-bookworm

# postgres:16-bookworm NO trae PostGIS: hay que instalar la extensión
# (postgresql-16-postgis-3) además de los binarios de cliente (postgis),
# y crearla al inicializar un volumen nuevo, como hacía postgis/postgis.
RUN apt-get update \
 && apt-get install -y --no-install-recommends gdal-bin postgis postgresql-16-postgis-3 \
 && rm -rf /var/lib/apt/lists/* \
 && echo "CREATE EXTENSION IF NOT EXISTS postgis;" > /docker-entrypoint-initdb.d/10_postgis.sql
