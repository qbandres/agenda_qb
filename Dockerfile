# Usamos una imagen base oficial y ligera de Python
# Esta imagen es "Multi-arch", funciona en ARM (Mac) y AMD64 (Linux Server) automáticamente
FROM python:3.11-slim

# Evita que Python escriba archivos .pyc y bufferice logs
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Instalamos dependencias del sistema necesarias para PostgreSQL y compilación
# (gcc y libpq-dev son vitales para que psycopg2 no falle al compilar)
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Primero copiamos solo los requirements para aprovechar la caché de Docker
# Si solo cambia el código pero no los requerimientos, este paso se salta (es más rápido)
COPY requirements.txt .

# Instalamos las librerías
RUN pip install --no-cache-dir -r requirements.txt

# Copiamos el resto del código
COPY . .

# Comando de arranque
CMD ["python", "main.py"]
