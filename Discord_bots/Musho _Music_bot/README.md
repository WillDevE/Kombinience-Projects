# Musho Music Bot

A Discord music bot that can play audio from YouTube and directly download from Spotify.

## Features

- Play music from YouTube URLs
- Direct download and playback from Spotify (tracks, playlists, albums)
- Queue management
- Skip, pause, and resume functionality
- Web dashboard for monitoring playback and statistics

## Setup

1. Clone this repository
2. Create a Discord bot and get the token
3. Create a Spotify developer application and get credentials
4. Set up environment variables in `.env` file:

```
# Discord Bot Token
DISCORD_BOT_TOKEN=your_discord_bot_token_here

# Cobalt API Configuration (optional)
COBALT_API_URL=http://localhost:9000
COBALT_API_KEY=your_cobalt_api_key_here

# Spotify API Configuration
SPOTIFY_CLIENT_ID=your_spotify_client_id_here
SPOTIFY_CLIENT_SECRET=your_spotify_client_secret_here
SPOTIFY_REDIRECT_URI=http://localhost:8888/callback

# Bot Configuration
DEFAULT_VOLUME=0.2
MAX_SONG_LENGTH=900  # 15 minutes in seconds

# Dashboard Configuration
DASHBOARD_PORT=8080
DASHBOARD_URL_PREFIX=/musho

# Network Configuration (optional)
PROXY_URL=http://your-proxy-server:port
```

5. Install dependencies: `pip install -r requirements.txt`
6. Run the bot: `python musicbot.py`

## Docker Setup

Alternatively, you can run the bot using Docker:

```
docker-compose up -d
```

## Commands

- `/play <url>` - Play audio from YouTube URL or Spotify URL
- `/spotify <url>` - Play specifically from Spotify (track, playlist, or album)
- `/skip` - Skip the current song
- `/queue` - Display the current queue
- `/clear` - Clear the queue
- `/pause` - Pause the current song
- `/resume` - Resume playback

## Spotify Support

The bot now directly handles Spotify integration using the internal yt-dlp functionality, searching YouTube Music for the exact tracks without requiring external tools like spotify-dlp. This means:

1. Higher audio quality through better matching
2. More accurate metadata and album art
3. Direct support for Spotify's catalog
4. Proxy and cookie support for Spotify downloads

The bot can play from these Spotify URLs:
- Tracks: `https://open.spotify.com/track/...`
- Playlists: `https://open.spotify.com/playlist/...`
- Albums: `https://open.spotify.com/album/...`

For playlists and albums, the bot will add up to 25 tracks to the queue to avoid overwhelming the system.

## Web Dashboard

The bot includes a web dashboard accessible at `http://your-server:8080/musho/` that provides:

- Real-time playback information
- Guild statistics and history
- Song play history and analytics 