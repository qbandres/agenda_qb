# agenda_qb (Segundo Cerebro - Telegram Bot)

Este es un bot de Telegram que actúa como tu "segundo cerebro", procesando mensajes de texto, imágenes (planos, documentos) y notas de voz para extraer información estructurada y guardarla en una base de datos PostgreSQL. Utiliza la API de OpenAI (GPT-4o + Whisper) para el procesamiento inteligente.

## Requisitos Previos

- Docker y Docker Compose instalados.
- Un token de bot de Telegram de [@BotFather](https://t.me/BotFather).
- Una API Key de OpenAI de [OpenAI Platform](https://platform.openai.com/).

## Configuración

1. **Clonar el repositorio** (o navegar a la carpeta del proyecto):
   ```bash
   # (Ya estás en la carpeta del proyecto)
   ```

2. **Configurar Variables de Entorno**:
   Copia el archivo de ejemplo y edítalo con tus claves reales.
   ```bash
   cp .env.example .env
   ```
   Edita `.env` y coloca tu `TELEGRAM_TOKEN` y `OPENAI_API_KEY`.

## Ejecución

Para iniciar el bot y la base de datos:

```bash
docker-compose up --build -d
```

El bot debería estar corriendo. Puedes ver los logs con:

```bash
docker-compose logs -f bot
```

## Uso

Envía mensajes al bot desde Telegram:
- **Texto**: Ideas, tareas, recordatorios.
- **Fotos**: Planos, tablas, documentos.
- **Audio**: Notas de voz con instrucciones o resúmenes.

El bot te responderá confirmando el guardado y mostrando un resumen de lo que entendió.

## Estructura de Base de Datos

Los datos se guardan en la tabla `agenda_personal` en PostgreSQL. Puedes conectarte a la base de datos usando cualquier cliente SQL con las credenciales definidas en `.env`.
