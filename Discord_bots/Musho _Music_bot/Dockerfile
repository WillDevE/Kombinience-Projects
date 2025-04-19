FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

# Create app directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    python3-dev \
    build-essential \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Create necessary directories with appropriate permissions
RUN mkdir -p /app/downloads /app/logs /app/templates /app/static /app/data && \
    chmod -R 777 /app/downloads /app/logs /app/data

# Install Python dependencies first (for better caching)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY *.py .
COPY *.mp3 .
COPY templates/ templates/
COPY static/ static/

# Expose dashboard port
EXPOSE 80

# Command to run the bot
CMD ["python", "musicbot.py"]