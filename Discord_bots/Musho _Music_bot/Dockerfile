FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for yt-dlp and ffmpeg
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers
RUN python -m playwright install --with-deps chromium

# Create downloads directory for songs
RUN mkdir -p downloads

# Copy the application code
COPY musicbot.py .
COPY .env .

# Optional leave sound
COPY leave.mp3 .

# Copy YouTube cookies file
COPY youtube_cookies.txt .

# Create logs directory
RUN mkdir -p logs

# Run the bot
CMD ["python", "musicbot.py"]