import subprocess
import psycopg2

DB_URL = "postgresql://postgres:pmd2026@localhost:5434/postgres"

def compose(*args):
    subprocess.run(["docker", "compose", *args], check=True)

def ejecutar_sql(conexion, sql):
    with conexion.cursor() as cur:
        cur.execute(sql)
    conexion.commit()

# --- 0. HABILITAR EXTENSIÓN HSTORE ---
print("Habilitando extensión hstore en la base de datos...")
conn_init = psycopg2.connect(DB_URL)
ejecutar_sql(conn_init, "CREATE EXTENSION IF NOT EXISTS hstore;")
conn_init.close()

# --- 1. CARGA DE OSM ---
print("Cargando OSM (puede tomar un par de minutos)...")
capas = {"points": "osm_puntos", "lines": "osm_lineas", "multipolygons": "osm_poligonos"}
PG_STR = "PG:host=localhost dbname=postgres user=postgres password=pmd2026"

for capa, tabla in capas.items():
    compose("exec", "-T", "db", "ogr2ogr", "-f", "PostgreSQL", PG_STR, 
            "/datos/osm_santiago-centro.gpkg", capa, "-nln", tabla, "-overwrite",
            "-lco", "GEOMETRY_NAME=geom", "-lco", "SPATIAL_INDEX=GIST",
            "-lco", "COLUMN_TYPES=other_tags=hstore", "-lco", "FID=id")

# --- 2. CARGA DE DPA (Comunas) ---
print("Cargando comunas_cl...")
compose("exec", "-T", "db", "psql", "-U", "postgres", "-d", "postgres", "-f", "/datos/comunas_cl.sql")

conn = psycopg2.connect(DB_URL)

# --- 3. DERIVACIÓN Y REPARACIÓN OSM ---
print("Reparando y derivando tablas de OSM...")

# 3.1 Contar reparaciones para el README (Cumple con la rúbrica)
with conn.cursor() as cur:
    cur.execute("SELECT count(*) FROM osm_poligonos WHERE NOT ST_IsValid(geom);")
    invalidos = cur.fetchone()[0]
    print(f"ATENCIÓN PARA EL README: Se detectaron {invalidos} polígonos inválidos antes de la reparación.")

# 3.2 Ejecutar la reparación y derivar las capas
ejecutar_sql(conn, """
    UPDATE osm_poligonos
    SET geom = ST_Multi(ST_CollectionExtract(ST_MakeValid(geom), 3))
    WHERE NOT ST_IsValid(geom);

    DROP TABLE IF EXISTS edificios, calles, paraderos_osm, amenidades CASCADE;
    
    CREATE TABLE edificios AS
    SELECT id, coalesce(osm_id, osm_way_id) AS osm_id, name, building,
           ST_Area(geom) AS area_m2, geom, ST_PointOnSurface(geom) AS punto
    FROM osm_poligonos WHERE building IS NOT NULL AND NOT ST_IsEmpty(geom);
    CREATE INDEX idx_edificios_geom ON edificios USING GIST (geom);
    CREATE INDEX idx_edificios_punto ON edificios USING GIST (punto);

    CREATE TABLE paraderos_osm AS
    SELECT id, osm_id, name, geom FROM osm_puntos WHERE highway = 'bus_stop';
    CREATE INDEX idx_paraderos_osm_geom ON paraderos_osm USING GIST (geom);

    CREATE TABLE amenidades AS
    SELECT id, osm_id, name, other_tags -> 'amenity' AS amenity, geom
    FROM osm_puntos WHERE other_tags ? 'amenity'
    UNION ALL
    SELECT id, coalesce(osm_id, osm_way_id), name, amenity, ST_PointOnSurface(geom)
    FROM osm_poligonos WHERE amenity IS NOT NULL AND NOT ST_IsEmpty(geom);
    CREATE INDEX idx_amenidades_geom ON amenidades USING GIST (geom);
""")
# --- 4. CARGA GTFS MASIVA CON COPY ---
print("Cargando CSVs del GTFS y construyendo geometrías...")

def cargar_csv_dinamico(cursor, archivo, nombre_tabla):
    """Lee el encabezado del CSV y crea la tabla cruda dinámicamente."""
    cursor.execute(f"DROP TABLE IF EXISTS {nombre_tabla};")
    with open(archivo, 'r', encoding='utf-8-sig') as f:
        # Extraemos el encabezado y limpiamos las comillas
        encabezados = f.readline().strip().replace('"', '').split(',')
        columnas = [f"{col} VARCHAR" for col in encabezados]
        
        cursor.execute(f"CREATE TABLE {nombre_tabla} ({', '.join(columnas)});")
        f.seek(0) # Volvemos al inicio del archivo
        cursor.copy_expert(f"COPY {nombre_tabla} FROM STDIN WITH CSV HEADER", f)

with conn.cursor() as cur:
    # 4.1 Carga de tablas crudas
    cargar_csv_dinamico(cur, 'datos/stops.txt', 'stops_raw')
    cargar_csv_dinamico(cur, 'datos/shapes.txt', 'shapes_raw')
    cargar_csv_dinamico(cur, 'datos/trips.txt', 'trips_raw')
    cargar_csv_dinamico(cur, 'datos/routes.txt', 'routes_raw')

    # 4.2 Paraderos GTFS (stops) - Convertimos a NUMERIC al vuelo
    cur.execute("""
        DROP TABLE IF EXISTS paraderos_gtfs;
        CREATE TABLE paraderos_gtfs AS
        SELECT stop_id, stop_name, 
               ST_Transform(ST_SetSRID(ST_MakePoint(stop_lon::NUMERIC, stop_lat::NUMERIC), 4326), 5361) AS geom
        FROM stops_raw;
        CREATE INDEX idx_paraderos_gtfs_geom ON paraderos_gtfs USING GIST (geom);
    """)

    # 4.3 Trazados GTFS (shapes) y asociación con rutas
    cur.execute("""
        DROP TABLE IF EXISTS recorridos;
        CREATE TABLE recorridos AS
        WITH shape_geoms AS (
            SELECT shape_id,
                   ST_Transform(ST_SetSRID(ST_MakeLine(ST_MakePoint(shape_pt_lon::NUMERIC, shape_pt_lat::NUMERIC) ORDER BY shape_pt_sequence::INTEGER), 4326), 5361) AS geom
            FROM shapes_raw
            GROUP BY shape_id
        ),
        shape_rutas AS (
            SELECT DISTINCT t.shape_id, r.route_short_name
            FROM trips_raw t
            JOIN routes_raw r ON t.route_id = r.route_id
        )
        SELECT sg.shape_id, sr.route_short_name, sg.geom
        FROM shape_geoms sg
        LEFT JOIN shape_rutas sr ON sg.shape_id = sr.shape_id;
        
        CREATE INDEX idx_recorridos_geom ON recorridos USING GIST (geom);
    """)
# --- 5. ANALYZE FINAL ---
print("Ejecutando ANALYZE...")
ejecutar_sql(conn, "ANALYZE;")
conn.close()
print("¡ETL finalizado con éxito! Datos listos para consultar.")