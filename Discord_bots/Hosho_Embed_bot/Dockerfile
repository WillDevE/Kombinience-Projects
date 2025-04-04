FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for yt-dlp
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY embedbot.py .
COPY html_counter.txt .
COPY video_embed_template.html .
COPY .env .

# Create logs directory
RUN mkdir -p logs

# Run the bot
CMD ["python", "embedbot.py"] 