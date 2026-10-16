# Tarea 1: PostGIS de punta a punta - Santiago Centro

**Autor:** Alejandro Valenzuela  
**Paralelo:** 701  

## 1. Reproducción desde cero

Para ejecutar este pipeline geoespacial en una máquina limpia, sigue estos pasos:

1. Clona este repositorio (los datos fuente están excluidos vía `.gitignore`).
2. Descarga los datos fuente requeridos y ubícalos en la carpeta `datos/`:
   - `comunas_cl.sql` (Procesado previamente en el Lab 03).
   - `osm_santiago-centro.gpkg` (Extracto generado con `preparar_datos.py --zona santiago-centro`).
   - Archivos extraídos del GTFS: `shapes.txt`, `stops.txt`, `routes.txt` y `trips.txt`.
3. Levanta la infraestructura de base de datos con Docker:
   ```bash
   docker compose up -d --wait
   ```

4. Abre y ejecuta el Jupyter Notebook (consultas_tarea1.ipynb) del proyecto para instalar dependencias, realizar la carga ETL y ejecutar los análisis geoespaciales.

## 2. Fuentes de Datos y Fechas
* OpenStreetMap (OSM): Extracto de Chile descargado desde Geofabrik. El archivo base utilizado fue chile-latest.osm.pbf (descargado y procesado en octubre de 2026).

* GTFS de Santiago: Datos oficiales del transporte público obtenidos de DTPM. Se utilizó el archivo vigente descargado en octubre de 2026 (Archivo original: GTFS_20261013.zip).

* División Política Administrativa (DPA): Límite de comunas chilenas (2023) proveniente del Laboratorio 03.

## 3. Decisiones Técnicas del Pipeline
* Sistema de Referencia (SRID): Se forzó el uso de SRID 5361 (SIRGAS-Chile / UTM 19S) para todas las capas geométricas. Esto permite realizar cálculos de área (ST_Area) y distancia (ST_DWithin, ST_Distance) en metros reales, evitando las distorsiones de los grados de latitud/longitud.

* Punto interior vs. Polígono: Para la tabla edificios y amenidades derivadas de polígonos, se calculó y almacenó el punto interior utilizando ST_PointOnSurface. Esto es fundamental para evitar el doble conteo de estructuras que intersectan los bordes de dos comunas durante los cruces espaciales (joins).

* Reparación de Geometrías: Se detectaron 368 polígonos inválidos en los datos crudos de OSM. Se repararon utilizando la secuencia ST_Multi(ST_CollectionExtract(ST_MakeValid(geom), 3)) antes de derivar las capas finales para evitar que funciones espaciales fallaran.

* Trazados Múltiples de GTFS: El archivo shapes.txt contenía cientos de miles de puntos. Para evitar cuellos de botella, se volcó el archivo crudo en PostgreSQL usando COPY. Luego, las líneas se construyeron mediante ST_MakeLine(ST_MakePoint(...) ORDER BY shape_pt_sequence). Se utilizó un LEFT JOIN con trips_raw y routes_raw extrayendo un DISTINCT route_short_name por cada shape_id para asociar cada variante geométrica a su código de ruta comercial sin duplicar la línea base.

