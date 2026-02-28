# Jarvis — Agenda Personal (Bot de Telegram)

> Bot de Telegram que funciona como "segundo cerebro": clasifica, guarda y consulta información personal usando IA (GPT-4o + Whisper) y PostgreSQL.

---

## Índice

- [Arquitectura General](#arquitectura-general)
- [Estructura del Proyecto](#estructura-del-proyecto)
- [Flujo de Ejecución](#flujo-de-ejecución)
- [Base de Datos](#base-de-datos)
- [Funciones Documentadas](#funciones-documentadas)
- [Variables de Entorno](#variables-de-entorno)
- [Ejecución y Despliegue](#ejecución-y-despliegue)
- [Mejoras Pendientes](#mejoras-pendientes)

---

## Arquitectura General

```
[Usuario Telegram]
       |
       v
[python-telegram-bot]
       |
  ┌────┴────────────────────┐
  │     master_handler      │  ← Punto de entrada para mensajes
  └────┬────────────────────┘
       |
  ┌────▼────────────────────┐
  │   get_user_categories   │  ← Lee proyectos del usuario desde DB
  └────┬────────────────────┘
       |
  ┌────▼────────────────────┐
  │    process_with_ai      │  ← Llama a GPT-4o con contexto dinámico
  │   (texto / imagen / voz)│
  └────┬────────────────────┘
       |
  ┌────▼────────────────────┐
  │  Intención detectada    │
  │  SAVE / QUERY /         │
  │  UPDATE / DELETE        │
  └────┬────────────────────┘
       |
  ┌────▼────────────────────┐
  │   PostgreSQL            │  ← Almacenamiento persistente
  │   (agenda_personal)     │
  └─────────────────────────┘
```

**Stack tecnológico:**
| Componente | Tecnología |
|---|---|
| Bot Telegram | python-telegram-bot 20.6 |
| IA Texto/Imagen | OpenAI GPT-4o |
| IA Audio | OpenAI Whisper-1 |
| Base de datos | PostgreSQL (psycopg2) |
| Contenedor | Docker + Docker Compose |
| Runtime | Python 3.11 |

---

## Estructura del Proyecto

```
2026-agenda_personal/
├── main.py              # Punto de entrada: init_db + handlers + run_polling
├── config.py            # Variables compartidas: logger + client OpenAI
├── db.py                # get_db_connection, init_db, execute_sql, get_user_categories
├── ai.py                # get_system_prompt, process_with_ai
├── handlers.py          # start, master_handler, show_save_confirmation, button_callback
├── utils.py             # escape_markdown, encode_image
├── requirements.txt     # Dependencias Python
├── Dockerfile           # Imagen Docker (python:3.11-slim, multi-arch)
├── docker-compose.yml   # Servicio bot + red externa (home-server-net)
├── README.md            # Guía básica de instalación
└── DOCUMENTACION.md     # Este archivo
```

### Dependencias entre módulos

```
main.py
  ├── config.py       (sin deps internas)
  ├── db.py           → config.py
  ├── utils.py        (sin deps internas)
  ├── ai.py           → config.py, utils.py
  └── handlers.py     → config.py, db.py, ai.py, utils.py
```

---

## Flujo de Ejecución

### 1. Arranque (`__main__`)
```
init_db()           → Crea/migra tablas en PostgreSQL
ApplicationBuilder  → Inicializa el bot con TELEGRAM_TOKEN
Handlers registrados:
  - /start           → CommandHandler → start()
  - Texto|Foto|Voz   → MessageHandler → master_handler()
  - Botones inline   → CallbackQueryHandler → button_callback()
app.run_polling()   → Inicia el loop de eventos
```

### 2. Mensaje entrante → `master_handler()`
```
1. Obtiene user_id y username
2. Llama get_user_categories(username) → lista de proyectos válidos
3. Detecta tipo de contenido:
   - TEXT  → process_with_ai('text', ...)
   - PHOTO → descarga imagen temp → process_with_ai('image', ...)
   - VOICE → descarga audio temp  → process_with_ai('audio', ...)
4. Evalúa intent de la respuesta IA:
   - SAVE   → show_save_confirmation()
   - QUERY  → execute_sql() → formatea y muestra resultados
   - DELETE/UPDATE → muestra preview + botones de confirmación
```

### 3. Respuesta IA → Intenciones
| Intent | Acción |
|---|---|
| `SAVE` | Muestra tarjeta de confirmación con botones Confirmar/Editar/Descartar |
| `QUERY` | Ejecuta SQL de búsqueda y formatea los resultados agrupados por categoría |
| `UPDATE` | Muestra SQL propuesto + filas afectadas, espera confirmación |
| `DELETE` | Muestra SQL propuesto + filas afectadas, espera confirmación |

### 4. Callbacks de botones → `button_callback()`
| `callback_data` | Acción |
|---|---|
| `save` | INSERT en `agenda_personal` con estado `APPROVED` |
| `edit` | Cambia state a `WAITING_EDIT`, espera nuevo mensaje |
| `cancel` | Cancela y limpia `context.user_data` |
| `exec_sql` | Ejecuta el SQL pendiente (UPDATE/DELETE) |

---

## Base de Datos

### Tabla `agenda_personal`
| Columna | Tipo | Descripción |
|---|---|---|
| `id` | SERIAL PK | Identificador único autoincremental |
| `telegram_user_id` | BIGINT | ID numérico del usuario en Telegram |
| `username` | VARCHAR(100) | Username o nombre del usuario |
| `fecha_creacion` | TIMESTAMP | Fecha de registro (auto: NOW()) |
| `categoria` | VARCHAR(50) | Categoría principal (ej: OBRA, LIBRE) |
| `subcategoria` | VARCHAR(100) | Subcategoría / proyecto específico |
| `tipo_entrada` | VARCHAR(50) | TAREA / RECORDATORIO / NOTA / CULTURA / GASTO |
| `resumen` | TEXT | Resumen corto del registro |
| `contenido_completo` | TEXT | Contenido detallado / transcripción completa |
| `fecha_evento` | TIMESTAMP | Fecha del evento (puede ser null) |
| `datos_extra` | JSONB | Datos adicionales en formato JSON |
| `estado` | VARCHAR(20) | `Open` (default) / `APPROVED` / `Closed` |

### Tabla `categorias_agenda`
> Tabla de configuración por usuario. **Debe crearse manualmente** (no incluida en `init_db`).

| Columna | Tipo | Descripción |
|---|---|---|
| `username` | VARCHAR | Username del usuario Telegram |
| `categoria` | VARCHAR | Categoría principal |
| `subcategoria` | VARCHAR | Subcategoría / proyecto |
| `estado` | VARCHAR | `ACTIVO` / `INACTIVO` |

---

## Funciones Documentadas

### Base de Datos

#### `get_db_connection()` → `psycopg2.connection`
Crea y retorna una nueva conexión a PostgreSQL usando variables de entorno.
```
Vars usadas: POSTGRES_HOST, POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_PORT
```

#### `init_db()` → `None`
Crea la tabla `agenda_personal` si no existe. Aplica migraciones para agregar columnas faltantes (`username`, `tipo_entrada`) usando `DO $$` de PostgreSQL.

#### `execute_sql(query: str)` → `list[dict] | int | None`
Ejecuta SQL arbitrario. Si la query retorna filas (SELECT), devuelve lista de dicts. Si no (INSERT/UPDATE/DELETE), devuelve el `rowcount`. Retorna `None` en caso de error.

#### `get_user_categories(username: str)` → `str`
Consulta `categorias_agenda` para el usuario y construye un bloque de texto con la lista de proyectos válidos. Este texto se inyecta en el prompt del sistema de la IA.
- Sin categorías → retorna instrucción de usar `LIBRE`
- Con categorías → retorna mapa `categoria: [subcategorias]`

---

### Inteligencia Artificial

#### `get_system_prompt(user_id, username, categorias_dinamicas)` → `str`
Genera el prompt del sistema para GPT-4o. Incluye:
- Identidad ("Jarvis", clasificador rígido)
- Lista dinámica de categorías del usuario
- Reglas de mapeo y SQL
- Formato JSON esperado en la respuesta

**Formato de respuesta JSON esperado de la IA:**
```json
{
  "intent": "SAVE | QUERY | DELETE | UPDATE",
  "reasoning": "Explicación del mapeo",
  "sql_query": "SELECT/UPDATE/DELETE ...",
  "save_data": {
    "category": "CATEGORIA",
    "subcategory": "SUBCATEGORIA",
    "entry_type": "TAREA|RECORDATORIO|NOTA|CULTURA|GASTO",
    "summary": "Resumen corto",
    "full_content": "Contenido completo",
    "event_date": "YYYY-MM-DD HH:MM:SS o null",
    "extra_data": {},
    "status": "Open"
  },
  "user_reply": "Mensaje de confirmación para el usuario"
}
```

#### `process_with_ai(content_type, content_data, current_date, user_id, username, categorias_dinamicas)` → `dict | None`
Orquesta la llamada a la API de OpenAI. Soporta 3 modos:

| `content_type` | Flujo |
|---|---|
| `'text'` | Envía el texto directamente como mensaje de usuario |
| `'audio'` | Transcribe con Whisper-1, luego envía la transcripción |
| `'image'` | Codifica en base64, envía con vision a GPT-4o |

Usa `temperature=0.0` y `response_format: json_object` para respuestas deterministas.

---

### Handlers de Telegram

#### `start(update, context)` → `None`
Responde al comando `/start`. Limpia `user_data` y saluda al usuario.

#### `master_handler(update, context)` → `None`
Handler principal. Procesa mensajes de texto, fotos y voz. Ver [Flujo de Ejecución](#flujo-de-ejecución) para detalle completo.
- Maneja el estado `WAITING_EDIT` para correcciones de datos

#### `show_save_confirmation(update, context, data)` → `None`
Muestra una tarjeta de confirmación con los datos extraídos por la IA antes de guardar. Incluye botones inline: Confirmar / Editar / Descartar.

#### `button_callback(update, context)` → `None`
Maneja todos los botones inline de la aplicación. Ver tabla de callbacks en [Flujo de Ejecución](#flujo-de-ejecución).

---

### Utilidades

#### `clean_and_parse_json(text_response: str)` → `dict | None`
Limpia bloques de código markdown (` ```json `) y parsea el JSON resultante.
> Actualmente sin uso activo en el flujo principal (el JSON se parsea directamente).

#### `escape_markdown(text: str)` → `str`
Escapa caracteres especiales de Markdown (`_`, `*`, `` ` ``, `[`) para evitar errores de `BadRequest` en la API de Telegram.

#### `encode_image(image_path: str)` → `str`
Lee un archivo de imagen y lo codifica en base64 para enviarlo a la API de visión de OpenAI.

---

## Variables de Entorno

Crear archivo `.env` en la raíz del proyecto:

```env
# Telegram
TELEGRAM_TOKEN=your_bot_token_from_botfather

# OpenAI
OPENAI_API_KEY=sk-...

# PostgreSQL
POSTGRES_HOST=nombre_contenedor_o_ip
POSTGRES_DB=nombre_base_datos
POSTGRES_USER=usuario
POSTGRES_PASSWORD=contraseña
POSTGRES_PORT=5432
```

---

## Ejecución y Despliegue

### Requisitos
- Docker y Docker Compose
- Red Docker externa `home-server-net` (donde corre PostgreSQL)
- Token de Telegram ([@BotFather](https://t.me/BotFather))
- API Key de OpenAI

### Iniciar el bot
```bash
# Construir imagen e iniciar en segundo plano
docker-compose up --build -d

# Ver logs en tiempo real
docker-compose logs -f bot-local

# Detener
docker-compose down
```

### Desarrollo local (sin Docker)
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

### Red Docker
El `docker-compose.yml` usa la red externa `home-server-net`. Esta red debe existir previamente:
```bash
docker network create home-server-net
```

---

## Mejoras Pendientes

### Seguridad (prioritarias)
- [ ] **SQL Injection en `get_user_categories`**: el `username` se interpola directamente en la query. Usar queries parametrizadas con `%s`.
- [ ] **Validación del SQL generado por IA**: `execute_sql` ejecuta cualquier SQL. Agregar whitelist de operaciones permitidas.

### Arquitectura
- [x] Separar el código en módulos (`config.py`, `db.py`, `ai.py`, `handlers.py`, `utils.py`)
- [ ] Usar pool de conexiones (`psycopg2.pool`) en lugar de abrir/cerrar por operación
- [ ] Agregar creación de `categorias_agenda` en `init_db()`

### Operaciones
- [ ] Agregar `.env.example` al repositorio (referenciado en README pero no existe)
- [ ] Agregar `healthcheck` en `docker-compose.yml`
- [ ] Agregar logging a archivo con rotación
- [ ] Remover o usar `clean_and_parse_json()` (función muerta)
