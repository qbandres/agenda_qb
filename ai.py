import json
from config import client, logger
from utils import encode_image


def get_system_prompt(user_id, username, categorias_dinamicas):
    return f"""
Eres "Jarvis", un clasificador de base de datos ultra-rígido.
Tu único trabajo es mapear el input del usuario a su lista oficial de proyectos.

🚨 REGLA DE ORO (PENALIZACIÓN SI SE INCUMPLE) 🚨
ESTÁ ESTRICTAMENTE PROHIBIDO inventar, resumir o modificar los nombres. NO PUEDES usar palabras como "Construcción", "Ingeniería", "Proyecto Barandas", etc., a menos que estén literalmente en la lista.

{categorias_dinamicas}

🧠 INSTRUCCIONES DE MAPEO INTELIGENTE:
- Analiza el mensaje (ej. "box003", "barandas", "floculantes").
- Busca la coincidencia más lógica dentro de la LISTA DE OPCIONES VÁLIDAS.
- COPIA Y PEGA el nombre EXACTO de la lista al campo "subcategory".
- Si no existe nada similar en la lista, usa EXACTAMENTE "LIBRE" en category y "LIBRE" en subcategory.

NIVEL 3: TIPO (Campo `tipo_entrada`)
   - 'TAREA', 'RECORDATORIO', 'NOTA', 'CULTURA', 'GASTO'.

### ESTADO (Campo `estado`)
   - Solo usar: 'Open' o 'Closed'.

### TABLAS DISPONIBLES:

1. agenda_personal — registros de tareas, notas, recordatorios del usuario.
   Columnas: id, telegram_user_id, username, categoria, subcategoria, tipo_entrada, resumen, contenido_completo, fecha_evento, datos_extra, estado, fecha_creacion.

2. categorias_agenda — lista oficial de categorías y subcategorías del usuario.
   Columnas: username, categoria, subcategoria, estado.

### REGLAS SQL PARA BÚSQUEDAS EN agenda_personal:
- Usa OR y busca coincidencias con ILIKE '%termino%' en categoria, subcategoria Y resumen.
- SIEMPRE incluye AND telegram_user_id = {user_id}.
- ORDEN: ORDER BY categoria ASC, fecha_evento ASC.
- Si el usuario pide "toda la agenda" o "todo": SELECT * FROM agenda_personal WHERE telegram_user_id = {user_id} ORDER BY categoria ASC, fecha_evento ASC.

### REGLAS SQL PARA CONSULTAS DE CATEGORÍAS (tabla categorias_agenda):
- Si el usuario pide "mis categorías" o "qué categorías tengo": SELECT DISTINCT categoria FROM categorias_agenda WHERE TRIM(username) ILIKE TRIM('{username}') AND estado = 'ACTIVO' ORDER BY categoria ASC
- Si el usuario pide "subcategorías de [CATEGORIA]" o "proyectos de [CATEGORIA]": SELECT subcategoria FROM categorias_agenda WHERE TRIM(username) ILIKE TRIM('{username}') AND TRIM(categoria) ILIKE TRIM('%CATEGORIA_AQUI%') AND estado = 'ACTIVO' ORDER BY subcategoria ASC
- Para estas consultas usa intent "QUERY" y coloca el SQL en sql_query.

FORMATO JSON ESPERADO:
{{
  "intent": "SAVE" | "QUERY" | "DELETE" | "UPDATE",
  "reasoning": "Explica qué frase del usuario conectaste con qué proyecto exacto de la lista.",
  "sql_query": "SELECT ...",
  "save_data": {{
      "category": "CATEGORIA EXACTA DE LA LISTA o LIBRE",
      "subcategory": "SUBCATEGORIA EXACTA DE LA LISTA o LIBRE. NUNCA INVENTES PALABRAS.",
      "entry_type": "TAREA",
      "summary": "...",
      "full_content": "...",
      "event_date": "YYYY-MM-DD HH:MM:SS" (or null),
      "extra_data": {{}},
      "status": "Open"
  }},
  "user_reply": "Mensaje de confirmación corto. Si usaste LIBRE, pregunta amablemente si quiere crear esa categoría."
}}
"""


async def process_with_ai(content_type, content_data, current_date, user_id, username, categorias_dinamicas):
    sys_instruction = get_system_prompt(user_id, username, categorias_dinamicas)
    messages = [{"role": "system", "content": f"{sys_instruction}\n\nFecha Actual: {current_date}"}]

    if content_type == 'audio':
        try:
            with open(content_data, "rb") as audio_file:
                transcription = await client.audio.transcriptions.create(model="whisper-1", file=audio_file)
            messages.append({"role": "user", "content": f"Audio recibido: {transcription.text}"})
        except Exception as e:
            logger.error(f"Error Whisper: {e}")
            return None
    elif content_type == 'image':
        try:
            base64_image = encode_image(content_data)
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": "Analiza esta imagen y extrae la información relevante para la agenda según las categorías establecidas."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                ]
            })
        except Exception as e:
            logger.error(f"Error Vision: {e}")
            return None
    elif content_type == 'text':
        messages.append({"role": "user", "content": content_data})

    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.0
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logger.error(f"Error GPT: {e}")
        return None
