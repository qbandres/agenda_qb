import os
import psycopg2
from psycopg2.extras import Json
from config import logger


def get_db_connection():
    return psycopg2.connect(
        host=os.getenv('POSTGRES_HOST'),
        database=os.getenv('POSTGRES_DB'),
        user=os.getenv('POSTGRES_USER'),
        password=os.getenv('POSTGRES_PASSWORD'),
        port=os.getenv('POSTGRES_PORT', '5432')
    )


def init_db():
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Tabla Principal
        cur.execute("""
            CREATE TABLE IF NOT EXISTS agenda_personal (
                id SERIAL PRIMARY KEY,
                telegram_user_id BIGINT,
                username VARCHAR(100),
                fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                categoria VARCHAR(50),
                subcategoria VARCHAR(100),
                tipo_entrada VARCHAR(50),
                resumen TEXT,
                contenido_completo TEXT,
                fecha_evento TIMESTAMP,
                datos_extra JSONB,
                estado VARCHAR(20) DEFAULT 'Open'
            );
        """)

        # Tabla de categorías por usuario
        cur.execute("""
            CREATE TABLE IF NOT EXISTS categorias_agenda (
                id SERIAL PRIMARY KEY,
                username VARCHAR(100) NOT NULL,
                categoria VARCHAR(50) NOT NULL,
                subcategoria VARCHAR(100) NOT NULL,
                estado VARCHAR(20) DEFAULT 'ACTIVO'
            );
        """)

        # Asegurar columnas (migración)
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='agenda_personal' AND column_name='username') THEN
                    ALTER TABLE agenda_personal ADD COLUMN username VARCHAR(100);
                END IF;

                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='agenda_personal' AND column_name='tipo_entrada') THEN
                    ALTER TABLE agenda_personal ADD COLUMN tipo_entrada VARCHAR(50);
                END IF;
            END $$;
        """)

        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Error DB init: {e}")


async def execute_sql(query, params=None):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        logger.info(f"SQL Exec: {query} | Params: {params}")
        cur.execute(query, params)
        if cur.description:
            rows = cur.fetchall()
            cols = [desc[0] for desc in cur.description]
            result = [dict(zip(cols, row)) for row in rows]
        else:
            conn.commit()
            result = cur.rowcount
        conn.close()
        return result
    except Exception as e:
        logger.error(f"SQL Error: {e}")
        return None


async def get_user_categories(username):
    query = "SELECT categoria, subcategoria FROM categorias_agenda WHERE TRIM(username) ILIKE TRIM(%s) AND estado = 'ACTIVO'"
    try:
        results = await execute_sql(query, (username,))

        logger.info(f"Buscando proyectos para: '{username}'. Encontrados: {len(results) if results else 0}")

        if not results:
            return "ESTE USUARIO NO TIENE LISTA. USA CATEGORIA 'LIBRE' Y SUBCATEGORIA 'LIBRE'."

        cat_map = {}
        for r in results:
            cat = r.get('categoria', '')
            sub = r.get('subcategoria', '')
            if cat not in cat_map:
                cat_map[cat] = []
            cat_map[cat].append(f'"{sub}"')

        prompt_text = "LISTA DE OPCIONES VÁLIDAS POR CATEGORÍA:\n"
        for cat, subs in cat_map.items():
            prompt_text += f"- Si category es '{cat}', subcategory DEBE SER EXACTAMENTE UNA DE ESTAS: [{', '.join(subs)}]\n"

        return prompt_text
    except Exception as e:
        logger.error(f"Error cargando categorías: {e}")
        return "USA 'LIBRE'"
