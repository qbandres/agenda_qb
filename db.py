import os
import psycopg2
from psycopg2.extras import Json
from config import logger


def get_db_connection():
    conn = psycopg2.connect(
        host=os.getenv('POSTGRES_HOST'),
        database=os.getenv('POSTGRES_DB'),
        user=os.getenv('POSTGRES_USER'),
        password=os.getenv('POSTGRES_PASSWORD'),
        port=os.getenv('POSTGRES_PORT', '5432'),
        options="-c timezone=America/Lima"
    )
    return conn


DEFAULT_CATEGORIES = {
    "TRABAJO": ["General", "Reuniones", "Pendientes", "Proyectos"],
    "ENTRETENIMIENTO": ["Películas", "Series", "Música", "Libros", "Videojuegos"],
    "PERSONAL": ["Salud", "Finanzas", "Compras", "Hogar", "Familia"],
    "RECORDATORIO": ["Citas", "Pagos", "Cumpleaños", "Trámites"],
}


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

        # Tabla de usuarios
        cur.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id SERIAL PRIMARY KEY,
                telegram_user_id BIGINT UNIQUE NOT NULL,
                username VARCHAR(100),
                nombre VARCHAR(200),
                fecha_registro TIMESTAMP DEFAULT NOW(),
                estado VARCHAR(20) DEFAULT 'ACTIVO'
            );
        """)

        # Tabla de categorías por usuario
        cur.execute("""
            CREATE TABLE IF NOT EXISTS categorias_agenda (
                id SERIAL PRIMARY KEY,
                telegram_user_id BIGINT NOT NULL,
                username VARCHAR(100),
                categoria VARCHAR(50) NOT NULL,
                subcategoria VARCHAR(100) NOT NULL,
                estado VARCHAR(20) DEFAULT 'ACTIVO'
            );
        """)

        # Migraciones
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='agenda_personal' AND column_name='username') THEN
                    ALTER TABLE agenda_personal ADD COLUMN username VARCHAR(100);
                END IF;

                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='agenda_personal' AND column_name='tipo_entrada') THEN
                    ALTER TABLE agenda_personal ADD COLUMN tipo_entrada VARCHAR(50);
                END IF;

                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='categorias_agenda' AND column_name='telegram_user_id') THEN
                    ALTER TABLE categorias_agenda ADD COLUMN telegram_user_id BIGINT;
                END IF;

                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='agenda_personal' AND column_name='notificaciones_enviadas') THEN
                    ALTER TABLE agenda_personal ADD COLUMN notificaciones_enviadas JSONB DEFAULT '[]'::jsonb;
                END IF;
            END $$;
        """)

        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Error DB init: {e}")


def register_user(telegram_user_id, username, nombre):
    """Registra un usuario nuevo o actualiza sus datos si ya existe. Retorna True si es nuevo."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("SELECT id FROM usuarios WHERE telegram_user_id = %s", (telegram_user_id,))
        existing = cur.fetchone()

        if existing:
            cur.execute(
                "UPDATE usuarios SET username = %s, nombre = %s WHERE telegram_user_id = %s",
                (username, nombre, telegram_user_id)
            )
            conn.commit()
            cur.close()
            conn.close()
            return False

        cur.execute(
            "INSERT INTO usuarios (telegram_user_id, username, nombre) VALUES (%s, %s, %s)",
            (telegram_user_id, username, nombre)
        )

        for categoria, subcategorias in DEFAULT_CATEGORIES.items():
            for sub in subcategorias:
                cur.execute("""
                    INSERT INTO categorias_agenda (telegram_user_id, username, categoria, subcategoria)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                """, (telegram_user_id, username, categoria, sub))

        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Error registro usuario: {e}")
        return False


def is_user_registered(telegram_user_id):
    """Verifica si un usuario está registrado y activo."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT estado FROM usuarios WHERE telegram_user_id = %s", (telegram_user_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row is not None and row[0] == 'ACTIVO'
    except Exception as e:
        logger.error(f"Error verificando usuario: {e}")
        return False


def get_upcoming_reminders(minutes_before, tolerance=1):
    """Busca eventos que estén a 'minutes_before' minutos de ocurrir (±tolerance)."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        label = f"{minutes_before}m"
        min_minutes = minutes_before - tolerance
        max_minutes = minutes_before + tolerance
        cur.execute("""
            SELECT id, telegram_user_id, categoria, subcategoria, resumen, fecha_evento
            FROM agenda_personal
            WHERE fecha_evento IS NOT NULL
              AND estado != 'Closed'
              AND fecha_evento BETWEEN NOW() + (%s * INTERVAL '1 minute')
                                    AND NOW() + (%s * INTERVAL '1 minute')
              AND NOT COALESCE(notificaciones_enviadas, '[]'::jsonb) @> %s::jsonb
        """, (min_minutes, max_minutes, f'["{label}"]'))
        cols = [desc[0] for desc in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        logger.info(f"Recordatorios [{label}]: {len(rows)} encontrados (ventana: {min_minutes}-{max_minutes} min)")
        cur.close()
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"Error buscando recordatorios: {e}")
        return []


def mark_reminder_sent(record_id, label):
    """Marca un recordatorio como enviado para no repetirlo."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            UPDATE agenda_personal
            SET notificaciones_enviadas = COALESCE(notificaciones_enviadas, '[]'::jsonb) || %s::jsonb
            WHERE id = %s
        """, (f'["{label}"]', record_id))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Error marcando recordatorio: {e}")


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


async def get_user_categories(telegram_user_id):
    query = "SELECT categoria, subcategoria FROM categorias_agenda WHERE telegram_user_id = %s AND estado = 'ACTIVO'"
    try:
        results = await execute_sql(query, (telegram_user_id,))

        logger.info(f"Buscando proyectos para user_id: {telegram_user_id}. Encontrados: {len(results) if results else 0}")

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
