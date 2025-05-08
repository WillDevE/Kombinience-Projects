# Discord Music Bot

A feature-rich Discord music bot with Spotify integration and a web dashboard.

## Features

- Play music from YouTube and Spotify (tracks, albums, playlists)
- Queue management with download pipeline
- Auto-leave voice channels when alone for 30 seconds
- Web dashboard for music statistics
- Robust download queue management (max 10 songs in buffer)
- YouTube cookies support for accessing age-restricted content

## Setup

### Environment Variables

Copy the example environment file and fill in your credentials:

```bash
cp .env.example .env
```

Edit `.env` with your Discord bot token, Spotify API credentials, and other settings.

### Running with Docker (Recommended)

The easiest way to run this bot is with Docker:

1. **Prerequisites:**
   - Install [Docker](https://docs.docker.com/get-docker/)
   - Install [Docker Compose](https://docs.docker.com/compose/install/)

2. **Build and start the bot:**
   ```bash
   docker-compose up -d
   ```

3. **View logs:**
   ```bash
   docker-compose logs -f
   ```

4. **Stop the bot:**
   ```bash
   docker-compose down
   ```

### Running Without Docker

If you want to run the bot directly:

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Install FFmpeg:**
   - Windows: Download from [ffmpeg.org](https://ffmpeg.org/download.html) and add to PATH
   - macOS: `brew install ffmpeg`
   - Linux: `sudo apt install ffmpeg`

3. **Run the bot:**
   ```bash
   python musicbot.py
   ```

## Dashboard

The web dashboard is available at:
http://localhost:8080/musho/ 

(Adjust the port and URL prefix if you changed them in the .env file)

## Commands

- `/play <url>` - Play a YouTube URL or Spotify link (track/album/playlist)
- `/queue` - Display the current queue
- `/skip` - Skip the current song
- `/pause` - Pause playback
- `/resume` - Resume playback
- `/clear` - Clear the queue
- `/setcookies` - Set YouTube cookies for accessing age-restricted content

## Troubleshooting

- If you encounter issues with Spotify integration, verify your Spotify API credentials.
- For playback issues, ensure FFmpeg is installed correctly.
- If the bot can't join voice channels, check your Discord bot permissions.

## Docker Management

- **Update the bot:**
  ```bash
  docker-compose pull
  docker-compose up -d --build
  ```

- **View container stats:**
  ```bash
  docker stats discord-music-bot
  ```

- **Access container shell:**
  ```bash
  docker-compose exec music-bot bash
  ```

- **Clear persistent data:**
  ```bash
  docker-compose down
  rm -rf ./downloads/* ./data/*
  docker-compose up -d
  ``` 