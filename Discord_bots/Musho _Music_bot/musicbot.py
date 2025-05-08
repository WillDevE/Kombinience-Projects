import asyncio
import json
import logging
import os
import shutil
from collections import defaultdict
from typing import Optional, Tuple, List, Dict, Union
import itertools
import re
import subprocess
import urllib.parse
import time

import discord
import aiohttp
from discord.ext import commands, tasks
from discord import app_commands
from fake_useragent import UserAgent
import yt_dlp
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

# Import dashboard module
from dashboard import register_bot, record_song_played, start_dashboard, update_stats, save_dashboard_data

# Load environment variables
load_dotenv()

# Configure logging with timestamp and level
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Constants
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
# Path to YouTube cookies file
YOUTUBE_COOKIES = os.path.join(os.path.dirname(__file__), "youtube_cookies.txt")
# Path to a writable copy of the cookies file
YOUTUBE_COOKIES_WRITABLE = os.path.join(os.path.dirname(__file__), "downloads", "yt_cookies_writable.txt")
MAX_RETRY_ATTEMPTS = 3
RETRY_DELAY = 1
DEFAULT_VOLUME = float(os.getenv("DEFAULT_VOLUME", "0.2"))
MAX_SONG_LENGTH = int(os.getenv("MAX_SONG_LENGTH", "7200"))  # 120 minutes in seconds
# Proxy URL (if needed)
PROXY_URL = os.getenv("PROXY_URL")

# New constants for download queue management
MAX_SONGS_IN_DOWNLOAD_BUFFER = 10  # Max songs (downloaded + downloading) per guild
DOWNLOAD_WORKER_CHECK_INTERVAL = 1 # Seconds for worker loop

# Spotify Configuration
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI")

# Spotify URL patterns - improved to handle all possible Spotify URL formats
SPOTIFY_TRACK_URL_PATTERN = r'https?://open\.spotify\.com/track/([a-zA-Z0-9]+)(\?.*)?'
SPOTIFY_PLAYLIST_URL_PATTERN = r'https?://open\.spotify\.com/playlist/([a-zA-Z0-9]+)(\?.*)?'
SPOTIFY_ALBUM_URL_PATTERN = r'https?://open\.spotify\.com/album/([a-zA-Z0-9]+)(\?.*)?'
# Also handle shortened URLs
SPOTIFY_SHORT_URL_PATTERN = r'https?://spotify\.link/([a-zA-Z0-9]+)'

class Song:
    def __init__(self, filename: str, title: str, duration: str, url: str, thumbnail: str):
        self.filename = filename
        self.title = title
        self.duration = duration
        self.url = url
        self.thumbnail = thumbnail
        self.playlist_info = None  # Optional playlist metadata

    @property
    def tuple(self) -> tuple:
        return (self.filename, self.title, self.duration, self.url, self.thumbnail)

class SpotifyClient:
    def __init__(self):
        """Initialize the Spotify client with credentials from environment variables."""
        if not all([SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET]):
            self.client = None
            logger.warning("Spotify credentials not found. Spotify support will be disabled.")
        else:
            # Initialize spotipy client for metadata fetching
            self.client = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
                client_id=SPOTIFY_CLIENT_ID,
                client_secret=SPOTIFY_CLIENT_SECRET
            ))
            
            logger.info("Spotify client initialized successfully.")
            
    # Define a Spotify Item class similar to spotify-dlp implementation
    class SpotifyItem:
        def __init__(self, item={}):
            self.id = item.get("id")
            self.type = item.get("type")
            self.title = item.get("name")
            self.index = 0
            
            if self.type == "track":
                self.authors = [author["name"] for author in item["artists"]]
                self.album = item["album"]["name"] if "album" in item else None
                self.date = item["album"]["release_date"] if "album" in item and "release_date" in item["album"] else None
                self.cover = item["album"]["images"][0]["url"] if "album" in item and "images" in item["album"] and item["album"]["images"] else None
                self.entry = item["track_number"] if "track_number" in item else None
            else:
                self.authors = []
                self.album = None
                self.date = None
                self.cover = None
                self.entry = None
        
        @property
        def url(self):
            return f"https://open.spotify.com/{self.type}/{self.id}"
            
        @property
        def keywords(self):
            return urllib.parse.quote_plus(f"{self.title} {' '.join(self.authors)}")

    def is_available(self) -> bool:
        """Check if Spotify client is available for use."""
        return self.client is not None

    def is_spotify_url(self, url: str) -> bool:
        """Check if the URL is a Spotify URL."""
        return bool(re.match(SPOTIFY_TRACK_URL_PATTERN, url)) or \
               bool(re.match(SPOTIFY_PLAYLIST_URL_PATTERN, url)) or \
               bool(re.match(SPOTIFY_ALBUM_URL_PATTERN, url))

    def get_track_type(self, url: str) -> Optional[str]:
        """Determine the type of Spotify URL (track, playlist, album)."""
        if re.match(SPOTIFY_TRACK_URL_PATTERN, url):
            return "track"
        elif re.match(SPOTIFY_PLAYLIST_URL_PATTERN, url):
            return "playlist"
        elif re.match(SPOTIFY_ALBUM_URL_PATTERN, url):
            return "album"
        return None

    def get_track_id(self, url: str) -> Optional[str]:
        """Extract the track ID from a Spotify track URL."""
        match = re.match(SPOTIFY_TRACK_URL_PATTERN, url)
        if match:
            return match.group(1)
        return None

    def get_playlist_id(self, url: str) -> Optional[str]:
        """Extract the playlist ID from a Spotify playlist URL."""
        match = re.match(SPOTIFY_PLAYLIST_URL_PATTERN, url)
        if match:
            return match.group(1)
        return None

    def get_album_id(self, url: str) -> Optional[str]:
        """Extract the album ID from a Spotify album URL."""
        match = re.match(SPOTIFY_ALBUM_URL_PATTERN, url)
        if match:
            return match.group(1)
        return None

    def get_track_info(self, track_id: str) -> Optional[Dict]:
        """Get information about a track."""
        if not self.is_available():
            return None
        try:
            return self.client.track(track_id)
        except Exception as e:
            logger.error(f"Error getting track info from Spotify: {e}")
            return None
            
    def parse_url(self, url: str) -> tuple[str, str]:
        """Parse a Spotify URL to extract the type and ID."""
        try:
            return re.search(r"(?:open\.spotify\.com/|spotify:)([a-z]+)(?:/|:)(\w+)", url).groups()
        except AttributeError:
            logger.error(f"Invalid Spotify URL: {url}")
            return None, None
            
    def items_by_url(self, url: str) -> list:
        """Get Spotify items based on URL type (track, album, playlist)."""
        item_type, item_id = self.parse_url(url)
        if not item_type or not item_id:
            return []
            
        items = []
        
        try:
            match item_type:
                case "track":
                    result = self.client.track(item_id)
                    items.append(self.SpotifyItem(result))
                    
                case "album":
                    album = self.client.album(item_id)
                    album_tracks = self.client.album_tracks(item_id)
                    for item in album_tracks["items"]:
                        # Include album info with each track
                        item["album"] = {
                            "name": album["name"],
                            "release_date": album.get("release_date"),
                            "images": album.get("images", [])
                        }
                        items.append(self.SpotifyItem(item))
                        
                case "playlist":
                    results = []
                    playlist_tracks = self.client.playlist_tracks(item_id)
                    results.extend([item['track'] for item in playlist_tracks['items'] if item['track']])
                    
                    # Handle pagination for playlists with more than 100 tracks
                    while playlist_tracks['next']:
                        playlist_tracks = self.client.next(playlist_tracks)
                        results.extend([item['track'] for item in playlist_tracks['items'] if item['track']])
                        
                    for item in results:
                        items.append(self.SpotifyItem(item))
                        
                case _:
                    logger.warning(f"Unsupported Spotify item type: {item_type}")
            
            # Add index to each item
            for index, item in enumerate(items, start=1):
                item.index = index
                
            return items
        except Exception as e:
            logger.error(f"Error fetching Spotify items for {url}: {e}")
            return []

    async def download_track(self, url: str) -> Optional[Song]:
        """Download a track from Spotify using direct YT-DLP integration."""
        if not self.is_available():
            return None
            
        try:
            # Create a unique download directory for this track with proper permissions
            download_dir = os.path.join(os.getcwd(), "downloads", "spotify")
            
            # Ensure all parent directories exist with proper permissions
            try:
                # First ensure downloads directory exists
                os.makedirs(os.path.join(os.getcwd(), "downloads"), exist_ok=True)
                # Then create spotify subdirectory
                os.makedirs(download_dir, exist_ok=True)
                
                # Check if directory is writable
                if not os.access(download_dir, os.W_OK):
                    logger.warning(f"Directory {download_dir} exists but is not writable. Trying to fix permissions.")
                    # Try to make the directory writable
                    os.chmod(download_dir, 0o755)  # rwxr-xr-x
                    
                    # Check again
                    if not os.access(download_dir, os.W_OK):
                        # If still not writable, fallback to temp directory
                        import tempfile
                        download_dir = tempfile.mkdtemp(prefix="musho_spotify_")
                        logger.warning(f"Using temporary directory for downloads: {download_dir}")
            except PermissionError as pe:
                # If permission error persists, use system temp directory
                import tempfile
                download_dir = tempfile.mkdtemp(prefix="musho_spotify_")
                logger.warning(f"Permission error creating directory. Using temporary directory: {download_dir}")
            
            # Get track metadata using spotipy
            track_id = self.get_track_id(url)
            if not track_id:
                logger.error(f"Could not extract track ID from Spotify URL: {url}")
                return None
                
            track_info = self.get_track_info(track_id)
            if not track_info:
                logger.error(f"Could not get track info from Spotify: {url}")
                return None
            
            # Create Spotify item from track info
            spotify_item = self.SpotifyItem(track_info)
            
            # Prepare track details
            track_title = track_info['name']
            track_artist = track_info['artists'][0]['name']
            track_album = track_info['album']['name']
            duration_ms = track_info['duration_ms']
            minutes = int((duration_ms / 1000) // 60)
            seconds = int((duration_ms / 1000) % 60)
            duration_str = f"{minutes}:{seconds:02d}"
            thumbnail = track_info['album']['images'][0]['url'] if track_info['album']['images'] else None
            
            # Create a safe filename
            safe_filename = f"{track_artist} - {track_title}".replace('/', '_').replace('\\', '_') \
                                                           .replace(':', '_').replace('*', '_') \
                                                           .replace('?', '_').replace('"', '_') \
                                                           .replace('<', '_').replace('>', '_') \
                                                           .replace('|', '_')
            
            # The key insight from spotify-dlp: use YouTube Music search with the track details
            yt_search_url = f"https://music.youtube.com/search?q={spotify_item.keywords}#songs"
            logger.info(f"Searching YouTube Music for: {track_artist} - {track_title}")
            
            cookies_file = None
            if os.path.exists(YOUTUBE_COOKIES):
                try:
                    shutil.copy2(YOUTUBE_COOKIES, YOUTUBE_COOKIES_WRITABLE)
                    cookies_file = YOUTUBE_COOKIES_WRITABLE
                    logger.info(f"Created writable copy of YouTube cookies at: {cookies_file}")
                except Exception as e:
                    logger.warning(f"Failed to create writable cookies copy: {e}")
            
            # Configure yt-dlp with our existing setup
            ydl_opts = {
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'outtmpl': os.path.join(download_dir, f"{safe_filename}.%(ext)s"),
                'quiet': False,
                'no_warnings': True,
                'extract_flat': False,
                'noplaylist': True,
                'playlist_items': '1',  # Only download the first search result
                'format_sort': ['acodec:mp3:128'],
                'postprocessor_args': ['-ar', '44100'],
                'prefer_ffmpeg': True,
            }
            
            # Add proxy configuration if set in environment variables
            if PROXY_URL:
                logger.info(f"Using proxy for Spotify download: {PROXY_URL}")
                ydl_opts['proxy'] = PROXY_URL
            
            # Add cookies file only if it exists
            if cookies_file and os.path.exists(cookies_file):
                logger.info(f"Using YouTube cookies file for Spotify download: {cookies_file}")
                ydl_opts['cookiefile'] = cookies_file
                
            # Execute the download
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                logger.info(f"Downloading Spotify track via YouTube Music: {track_artist} - {track_title}")
                info = await asyncio.get_event_loop().run_in_executor(
                    ThreadPoolExecutor(1),
                    lambda: ydl.extract_info(yt_search_url, download=True)
                )
                
                if not info:
                    logger.error("No info returned from yt-dlp for Spotify track")
                    return None
                
                # Get the actual filename where the file was downloaded
                entries = info.get('entries', [])
                if not entries:
                    logger.error("No entries found in YouTube Music search results")
                    return None
                    
                filename = ydl.prepare_filename(entries[0])
                # Adjust filename for mp3 extension since we're extracting audio
                filename = filename.rsplit(".", 1)[0] + ".mp3"
                
                # Check if file exists
                if not os.path.exists(filename):
                    logger.error(f"Downloaded file not found at expected path: {filename}")
                    return None
                
                logger.info(f"Successfully downloaded Spotify track to: {filename}")
                
                # Create a Song object with the track's metadata
                return Song(
                    filename=filename,
                    title=f"{track_artist} - {track_title}",
                    duration=duration_str,
                    url=url,
                    thumbnail=thumbnail
                )
                
        except Exception as e:
            logger.error(f"Error downloading track from Spotify: {e}", exc_info=True)
            return None

    async def get_playlist_tracks(self, playlist_id: str) -> List[Dict]:
        """Get tracks in a playlist."""
        if not self.is_available():
            return []
        try:
            results = []
            playlist_tracks = self.client.playlist_tracks(playlist_id)
            results.extend([item['track'] for item in playlist_tracks['items'] if item['track']])

            # Handle pagination for playlists with more than 100 tracks
            while playlist_tracks['next']:
                playlist_tracks = self.client.next(playlist_tracks)
                results.extend([item['track'] for item in playlist_tracks['items'] if item['track']])

            return results
        except Exception as e:
            logger.error(f"Error getting playlist tracks from Spotify: {e}")
            return []

    async def get_album_tracks(self, album_id: str) -> List[Dict]:
        """Get tracks in an album."""
        if not self.is_available():
            return []
        try:
            results = []
            album_tracks = self.client.album_tracks(album_id)
            results.extend(album_tracks['items'])

            # Handle pagination for albums with more than 50 tracks
            while album_tracks['next']:
                album_tracks = self.client.next(album_tracks)
                results.extend(album_tracks['items'])

            return results
        except Exception as e:
            logger.error(f"Error getting album tracks from Spotify: {e}")
            return []

    async def download_playlist(self, playlist_id: str, max_tracks: int = 100, page: int = 1) -> List[Song]:
        """Download a playlist from Spotify with pagination support."""
        if not self.is_available():
            return []
            
        try:
            # Create a unique download directory for this playlist with proper permissions
            download_dir = os.path.join(os.getcwd(), "downloads", "spotify", f"playlist_{playlist_id}")
            os.makedirs(download_dir, exist_ok=True)
            
            # Get playlist info for better details
            playlist_info = self.client.playlist(playlist_id)
            playlist_name = playlist_info['name']
            playlist_total = playlist_info.get('tracks', {}).get('total', 0)
            playlist_url = f"https://open.spotify.com/playlist/{playlist_id}"
            
            # Calculate pagination
            max_tracks = 100
            start_track = (page - 1) * max_tracks + 1
            
            # Get all tracks metadata first
            playlist_tracks = await self.get_playlist_tracks(playlist_id)
            if not playlist_tracks:
                embed = discord.Embed(
                    title="No Tracks Found",
                    description=f"No tracks found in playlist.",
                    color=discord.Color.red()
                )
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                else:
                    await interaction.followup.send(embed=embed, ephemeral=True)
                return False
                
            # Apply pagination
            start_idx = (page - 1) * max_tracks
            end_idx = min(start_idx + max_tracks, len(playlist_tracks))
            playlist_tracks_page = playlist_tracks[start_idx:end_idx]
            
            if not playlist_tracks_page:
                embed = discord.Embed(
                    title="No Tracks Found",
                    description=f"Page {page} has no tracks.",
                    color=discord.Color.red()
                )
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                else:
                    await interaction.followup.send(embed=embed, ephemeral=True)
                return False
            
            logger.info(f"Processing {len(playlist_tracks_page)} tracks from playlist '{playlist_name}' (page {page})")
            
            # Download each track individually
            downloaded_songs = []
            for item in playlist_tracks_page:
                try:
                    song = await self.download_track(item.url)
                    if song:
                        # Add playlist info to song
                        song.playlist_info = {
                            'name': playlist_name,
                            'total_tracks': playlist_total,
                            'current_page': page,
                            'tracks_per_page': max_tracks
                        }
                        downloaded_songs.append(song)
                except Exception as e:
                    logger.error(f"Error downloading playlist track {item.title}: {e}")
                    continue
            
            logger.info(f"Downloaded {len(downloaded_songs)} songs from playlist: {playlist_name}")
            return downloaded_songs
            
        except Exception as e:
            logger.error(f"Error downloading playlist from Spotify: {e}", exc_info=True)
            return []
            
    async def download_album(self, album_id: str, max_tracks: int = 100, page: int = 1) -> List[Song]:
        """Download an album from Spotify with pagination support."""
        if not self.is_available():
            return []
            
        try:
            # Create a unique download directory for this album with proper permissions
            download_dir = os.path.join(os.getcwd(), "downloads", "spotify", f"album_{album_id}")
            os.makedirs(download_dir, exist_ok=True)
            
            # Get album info for better details
            album_info = self.client.album(album_id)
            album_name = album_info['name']
            album_artist = album_info['artists'][0]['name']
            album_total = album_info.get('total_tracks', 0)
            album_url = f"https://open.spotify.com/album/{album_id}"
            
            # Calculate pagination
            max_tracks = 100
            start_track = (page - 1) * max_tracks + 1
            
            # Calculate total number of pages
            total_tracks = len(await self.get_album_tracks(album_id))
            max_pages = (total_tracks + max_tracks - 1) // max_tracks  # Ceiling division
            
            # Get all album tracks first
            album_tracks = await self.get_album_tracks(album_id)
            if not album_tracks:
                embed = discord.Embed(
                    title="No Tracks Found",
                    description=f"No tracks found in album.",
                    color=discord.Color.red()
                )
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                else:
                    await interaction.followup.send(embed=embed, ephemeral=True)
                return False
                
            # Apply pagination
            start_idx = (page - 1) * max_tracks
            end_idx = min(start_idx + max_tracks, len(album_tracks))
            album_tracks_page = album_tracks[start_idx:end_idx]
            
            if not album_tracks_page:
                embed = discord.Embed(
                    title="No Tracks Found",
                    description=f"Page {page} has no tracks.",
                    color=discord.Color.red()
                )
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                else:
                    await interaction.followup.send(embed=embed, ephemeral=True)
                return False
            
            logger.info(f"Processing {len(album_tracks_page)} tracks from album '{album_name}' (page {page})")
            
            # Get album images for thumbnail
            album_image = None
            if album_info.get('images') and len(album_info['images']) > 0:
                album_image = album_info['images'][0].get('url')
            
            # Download each track individually
            downloaded_songs = []
            for item in album_tracks_page:
                try:
                    song = await self.download_track(item.url)
                    if song:
                        # If thumbnail is missing, use album image
                        if not song.thumbnail and album_image:
                            song.thumbnail = album_image
                            
                        # Add album info to song
                        song.playlist_info = {
                            'name': f"{album_artist} - {album_name}",
                            'total_tracks': album_total,
                            'current_page': page,
                            'tracks_per_page': max_tracks,
                            'is_album': True
                        }
                        downloaded_songs.append(song)
                except Exception as e:
                    logger.error(f"Error downloading album track {item.title}: {e}")
                    continue
            
            logger.info(f"Downloaded {len(downloaded_songs)} songs from album: {album_artist} - {album_name}")
            return downloaded_songs
            
        except Exception as e:
            logger.error(f"Error downloading album from Spotify: {e}", exc_info=True)
            return []

class QueueManager:
    def __init__(self):
        self.playback_queues = defaultdict(list)  # Stores Song objects ready for playback
        self.download_pipelines = defaultdict(asyncio.Queue)  # Stores (url, interaction, is_spotify, spotify_info_dict)
        self.active_downloads = defaultdict(dict)  # guild_id -> {url_or_unique_id: asyncio.Task}
        self.guild_download_workers = {} # guild_id -> asyncio.Task for _process_download_pipeline

        self.current_songs = {}
        self.file_use_count = defaultdict(int)
        self._queue_locks = defaultdict(asyncio.Lock) # Protects playback_queues and current_songs
        self._pipeline_locks = defaultdict(asyncio.Lock) # Protects download_pipelines and active_downloads
        self._cleanup_tasks = set()  # Track cleanup tasks

    async def submit_for_download(self, guild_id: int, url: str, interaction: discord.Interaction, is_spotify: bool = False, spotify_info: Optional[Dict] = None) -> None:
        """Adds a song request to the download pipeline and ensures the worker is running."""
        await self.download_pipelines[guild_id].put((url, interaction, is_spotify, spotify_info))
        logger.info(f"Submitted {url} to download pipeline for guild {guild_id}")
        await self._ensure_download_worker_running(guild_id)

    async def _ensure_download_worker_running(self, guild_id: int) -> None:
        """Ensures the download worker task for a guild is running if not already."""
        async with self._pipeline_locks[guild_id]:
            if guild_id not in self.guild_download_workers or self.guild_download_workers[guild_id].done():
                logger.info(f"Starting download worker for guild {guild_id}")
                self.guild_download_workers[guild_id] = asyncio.create_task(self._process_download_pipeline(guild_id))
                # Clean up reference to old task if it exists and is done
                if guild_id in self.guild_download_workers and self.guild_download_workers[guild_id].done():
                    del self.guild_download_workers[guild_id]

    async def _process_download_pipeline(self, guild_id: int) -> None:
        """Processes songs from the download pipeline, respecting buffer limits."""
        # Ensure we have the bot instance correctly.
        # self.bot should be set when QueueManager is initialized by MusicBot
        if not hasattr(self, 'bot') or not self.bot:
            logger.error(f"Bot instance not available in QueueManager for guild {guild_id}. Worker stopping.")
            # No need to continue if we can't access the bot
            if guild_id in self.guild_download_workers:
               del self.guild_download_workers[guild_id]
            return

        while True:
            can_download_more = False
            async with self._queue_locks[guild_id]: # Lock for reading playback_queues length
                async with self._pipeline_locks[guild_id]: # Lock for active_downloads and download_pipelines
                    if not self.download_pipelines[guild_id].empty() and \
                       (len(self.playback_queues[guild_id]) + len(self.active_downloads[guild_id])) < MAX_SONGS_IN_DOWNLOAD_BUFFER:
                        can_download_more = True
            
            if not can_download_more:
                # If pipeline is empty and no active downloads, worker can stop.
                async with self._pipeline_locks[guild_id]:
                    if self.download_pipelines[guild_id].empty() and not self.active_downloads[guild_id]:
                        logger.info(f"Download pipeline empty and no active downloads for guild {guild_id}. Worker stopping.")
                        if guild_id in self.guild_download_workers: # Check if key exists
                           del self.guild_download_workers[guild_id]
                        return 
                await asyncio.sleep(DOWNLOAD_WORKER_CHECK_INTERVAL)
                continue

            url, interaction, is_spotify, spotify_info = await self.download_pipelines[guild_id].get()
            unique_download_id = url # Or generate a more unique ID if needed

            async with self._pipeline_locks[guild_id]:
                if unique_download_id in self.active_downloads[guild_id]:
                    logger.warning(f"Download for {url} already active in guild {guild_id}. Skipping.")
                    self.download_pipelines[guild_id].task_done()
                    continue
            
            download_task = None
            song_object = None
            try:
                logger.info(f"Starting download for {url} in guild {guild_id}")
                if is_spotify:
                    download_task = asyncio.create_task(self.bot.spotify_client.download_track(url))
                else:
                    download_task = asyncio.create_task(self.bot._download_song(url))
                
                async with self._pipeline_locks[guild_id]:
                    self.active_downloads[guild_id][unique_download_id] = download_task

                song_object = await download_task

            except Exception as e:
                logger.error(f"Error downloading {url} in guild {guild_id}: {e}", exc_info=True)
                current_interaction = interaction # Use the interaction passed to the worker item
                if current_interaction and not current_interaction.response.is_done():
                    try:
                        await current_interaction.followup.send(f"❌ Failed to download: {url}. Error: {str(e)[:1000]}", ephemeral=True)
                    except discord.errors.NotFound:
                        logger.warning("Interaction not found for download error message.")
                    except discord.errors.HTTPException as http_e:
                        logger.warning(f"HTTPException sending download error (likely already responded): {http_e}")
                # No else for sending to channel here, as it might not be appropriate for background task errors.
            finally:
                async with self._pipeline_locks[guild_id]:
                    if unique_download_id in self.active_downloads[guild_id]:
                        del self.active_downloads[guild_id][unique_download_id]
                self.download_pipelines[guild_id].task_done()

            if song_object:
                logger.info(f"Successfully downloaded {song_object.title} for guild {guild_id}")
                await self._add_to_playback_queue(guild_id, song_object, interaction)
                
                # Check if playback should start
                # This requires access to the bot (self.bot) or MusicBot instance
                guild = self.bot.get_guild(guild_id)
                if guild and guild.voice_client and not guild.voice_client.is_playing():
                     # Critical: _play_next needs the interaction object for the *first* song being played,
                     # not necessarily the interaction of the song that just finished downloading.
                     # This logic needs careful handling. For now, pass the current song's interaction.
                    await self.bot._play_next(guild, interaction)
            
            await asyncio.sleep(0.1) # Brief yield

    async def _add_to_playback_queue(self, guild_id: int, song: Song, interaction: Optional[discord.Interaction]) -> None:
        """Adds a downloaded song to the playback queue and updates use count."""
        async with self._queue_locks[guild_id]:
            self.playback_queues[guild_id].append(song)
            self.file_use_count[song.filename] += 1
        
        logger.info(f"Added '{song.title}' to playback queue for guild {guild_id}. Position: {len(self.playback_queues[guild_id])}")
        
        # Send confirmation if interaction is provided (for individually added songs)
        # Playlist/album additions might have a summary message instead.
        if interaction and not interaction.response.is_done(): # Check if response is done
            # Check if this is from a playlist addition to avoid spamming messages
            is_playlist_song = song.playlist_info is not None

            if not is_playlist_song or (is_playlist_song and len(self.playback_queues[guild_id]) == 1 and not interaction.guild.voice_client.is_playing()):
                # Only send detailed "Track Added" if not part of a bulk add or if it's the first to start playing
                # This logic might need refinement based on how playlist messages are handled
                position = len(self.playback_queues[guild_id])
                embed = discord.Embed(
                    title="Track Added to Queue",
                    description=f"[{song.title}]({song.url}) (Position: {position})",
                    color=discord.Color.green()
                )
                if song.thumbnail:
                    embed.set_thumbnail(url=song.thumbnail)
                embed.add_field(name="Duration", value=song.duration)
                try:
                    # If original processing message exists, edit it. Otherwise, send new.
                    # This requires passing the original processing_message or finding it.
                    # For simplicity, sending a new followup if the original interaction is still valid.
                    await interaction.followup.send(embed=embed)
                except discord.errors.NotFound:
                     logger.warning(f"Interaction for adding {song.title} not found, can't send followup.")
                except discord.errors.InteractionResponded:
                     logger.info(f"Interaction for {song.title} already responded, sending to channel.")
                     await interaction.channel.send(embed=embed)

    async def get_next_song(self, guild_id: int) -> Optional[Song]:
        """Gets the next song from the playback queue and updates current_song."""
        async with self._queue_locks[guild_id]:
            if not self.playback_queues[guild_id]:
                self.current_songs.pop(guild_id, None)
                return None
            song = self.playback_queues[guild_id].pop(0)
            self.current_songs[guild_id] = song
            return song

    async def clear_guild_queue(self, guild_id: int) -> None:
        """Clears all queues, cancels downloads, and cleans up resources for a guild."""
        logger.info(f"Clearing queue and downloads for guild {guild_id}")
        
        try:
            # Cancel active downloads with proper error handling
            async with self._pipeline_locks[guild_id]:
                if guild_id in self.active_downloads:
                    for url, task in list(self.active_downloads[guild_id].items()): # Iterate over a copy
                        try:
                            if not task.done():
                                task.cancel()
                                logger.info(f"Cancelled download task for {url} in guild {guild_id}")
                        except Exception as e:
                            logger.error(f"Error cancelling task for {url}: {e}")
                    self.active_downloads[guild_id].clear()

                # Clear download pipeline
                if guild_id in self.download_pipelines:
                    while not self.download_pipelines[guild_id].empty():
                        try:
                            self.download_pipelines[guild_id].get_nowait()
                            self.download_pipelines[guild_id].task_done()
                        except asyncio.QueueEmpty:
                            break
                        except Exception as e:
                            logger.error(f"Error clearing download pipeline: {e}")
                            break
                    logger.info(f"Cleared download pipeline for guild {guild_id}")

            # Stop and clear download worker with proper error handling
            async with self._pipeline_locks[guild_id]: # Ensure lock for worker dict
                if guild_id in self.guild_download_workers:
                    worker = self.guild_download_workers.pop(guild_id, None)
                    try:
                        if worker and not worker.done():
                            worker.cancel()
                            logger.info(f"Cancelled download worker for guild {guild_id}")
                    except Exception as e:
                        logger.error(f"Error cancelling download worker: {e}")
            
            # Clear playback queue and current song, cleanup files
            async with self._queue_locks[guild_id]:
                cleanup_tasks = []
                # Use get() with default to avoid KeyErrors
                for song in self.playback_queues.get(guild_id, []):
                    cleanup_tasks.append(self.cleanup_file(song.filename)) # cleanup_file is async
                
                current_song = self.current_songs.pop(guild_id, None)
                if current_song:
                    cleanup_tasks.append(self.cleanup_file(current_song.filename))

                # Clear the queue safely
                if guild_id in self.playback_queues:
                    self.playback_queues[guild_id].clear()
                
                if cleanup_tasks:
                    await asyncio.gather(*cleanup_tasks, return_exceptions=True)
                    logger.info(f"Scheduled cleanup for files in guild {guild_id}")

            # Also reset any auto-leave timer for this guild
            if hasattr(self.bot, 'alone_since_timestamps') and guild_id in self.bot.alone_since_timestamps:
                del self.bot.alone_since_timestamps[guild_id]
                logger.info(f"Reset auto-leave timer for guild {guild_id} due to queue clear.")

            # Update dashboard statistics
            try:
                update_stats()
            except Exception as e:
                logger.error(f"Error updating stats after queue clear: {e}")
                
        except Exception as e:
            logger.error(f"Error clearing queue for guild {guild_id}: {e}", exc_info=True)

    async def remove_song_from_playback_queue(self, guild_id: int, index: int) -> Optional[Song]:
        """Removes a song from the playback queue by index."""
        async with self._queue_locks[guild_id]:
            if not self.playback_queues[guild_id] or not (0 <= index < len(self.playback_queues[guild_id])):
                return None
            song = self.playback_queues[guild_id].pop(index)
            # Note: File cleanup for removed song should be handled here or by caller
            # await self.cleanup_file(song.filename) # Decide if removal implies immediate cleanup
            return song

    async def add_song(self, guild_id: int, song: Song) -> None:
        async with self._queue_locks[guild_id]:
            self.playback_queues[guild_id].append(song) # Changed from self.queues
            self.file_use_count[song.filename] += 1
            # Pre-downloading is now handled by the download worker ensuring buffer is filled
            await self._ensure_download_worker_running(guild_id)

    async def remove_song(self, guild_id: int, index: int) -> Optional[Song]:
        async with self._queue_locks[guild_id]:
            if not self.playback_queues[guild_id]: # Changed from self.queues
                return None
            song = self.playback_queues[guild_id].pop(index) # Changed from self.queues
            # Cancel any pending download for this song - This logic is now different.
            # Downloads are managed by the pipeline. If a song is in playback_queues, it's already downloaded.
            # If removing from download_pipelines is needed, that's a different function.
            return song

    async def _cleanup_guild_resources(self, guild_id: int) -> None:
        """Clean up all file resources for songs in the playback queue for a guild"""
        try:
            # This is mostly covered by clear_guild_queue's file cleanup logic.
            # If called separately, it should iterate self.playback_queues.
            cleanup_tasks = []
            async with self._queue_locks[guild_id]:
                # Use get with default to avoid KeyError
                for song in self.playback_queues.get(guild_id, []):
                    try:
                        cleanup_tasks.append(self.cleanup_file(song.filename))
                    except Exception as e:
                        logger.error(f"Error scheduling cleanup for file {song.filename}: {e}")
            
            if cleanup_tasks:
                await asyncio.gather(*cleanup_tasks, return_exceptions=True)
                logger.info(f"Completed resource cleanup for guild {guild_id}")
        except Exception as e:
            logger.error(f"Error cleaning up resources for guild {guild_id}: {e}", exc_info=True)

    async def cleanup_file(self, filename: str) -> None:
        """Clean up a file when it's no longer needed"""
        if filename in self.file_use_count:
            self.file_use_count[filename] -= 1
            if self.file_use_count[filename] <= 0:
                cleanup_task = asyncio.create_task(
                    self._delayed_file_cleanup(filename),
                    name=f"cleanup_{filename}"
                )
                self._cleanup_tasks.add(cleanup_task)
                cleanup_task.add_done_callback(self._cleanup_tasks.discard)

    async def _delayed_file_cleanup(self, filename: str) -> None:
        """Delayed file cleanup with retries"""
        try:
            # Wait a short time before cleanup to ensure file is not in use
            await asyncio.sleep(1)
            retry_count = 0
            while retry_count < 3:
                try:
                    if os.path.exists(filename):
                        os.remove(filename)
                    del self.file_use_count[filename]
                    break
                except (PermissionError, OSError) as e:
                    retry_count += 1
                    if retry_count == 3:
                        logger.error(f"Failed to delete file {filename} after 3 attempts: {e}")
                    else:
                        await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Error in delayed file cleanup for {filename}: {e}")

class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.members = True
        intents.guilds = True
        
        super().__init__(command_prefix='/', intents=intents)
        self.queue_manager = QueueManager()
        self.queue_manager.bot = self # Provide bot instance to QueueManager
        self.spotify_client = SpotifyClient()
        self.spotify_client.bot = self # If SpotifyClient needs bot access, e.g. for config
        self.tree.on_error = self.on_tree_error
        self.alone_since_timestamps = {} # For auto-leave feature
        
        # Set up download directories with proper permissions
        try:
            downloads_dir = os.path.join(os.getcwd(), "downloads")
            spotify_dir = os.path.join(downloads_dir, "spotify")
            
            # Create main directories if they don't exist
            os.makedirs(downloads_dir, exist_ok=True)
            os.makedirs(spotify_dir, exist_ok=True)
            
            logger.info(f"Download directories set up at {downloads_dir}")
            
        except Exception as e:
            logger.error(f"Error setting up download directories: {e}")
        
        # Dashboard settings
        self.dashboard_enabled = True
        self.dashboard_port = int(os.getenv("DASHBOARD_PORT", "8080"))
        self.dashboard_url_prefix = os.getenv("DASHBOARD_URL_PREFIX", "/musho")
        self.dashboard_thread = None

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandInvokeError):
            error = error.original
        
        # Check if the interaction response is already done
        if not interaction.response.is_done():
            try:
                await interaction.response.send_message(f"An error occurred: {str(error)}", ephemeral=True)
            except discord.errors.NotFound:
                logger.warning("Attempted to respond to an unknown interaction.")
        else:
            logger.warning("Interaction response already done, cannot send error message.")
        
        logger.error(f"Error in command {interaction.command}: {error}")

    async def setup_hook(self) -> None:
        """Initialize bot settings after login"""
        logger.info(f"Logged in as {self.user}")
        await self.setup_commands()
        try:
            logger.info("Syncing command tree...")
            await self.tree.sync()
            logger.info("Command tree synced successfully!")
        except Exception as e:
            logger.error(f"Failed to sync command tree: {e}")

    async def close(self):
        logger.info("Shutting down bot...")
        save_dashboard_data()
        logger.info("Dashboard data saved before shutdown.")
        await super().close()

    async def on_ready(self):
        logger.info(f"Bot is ready and logged in as {self.user}")
        
        # Check Spotify API credentials
        if self.spotify_client.is_available():
            logger.info("Spotify API client initialized successfully")
        else:
            logger.warning("Spotify API credentials not configured. Spotify features will not work.")
            
        # Start web dashboard if enabled
        if self.dashboard_enabled:
            try:
                logger.info(f"Initializing dashboard on port {self.dashboard_port} with URL prefix {self.dashboard_url_prefix}")
                
                # Set environment variable to force Flask to bind to all interfaces
                os.environ["FLASK_RUN_HOST"] = "0.0.0.0"
                
                # Register this bot instance with the dashboard
                register_bot(self)
                
                # Start dashboard in background
                self.dashboard_thread = start_dashboard(
                    host='0.0.0.0', 
                    port=self.dashboard_port,
                    url_prefix=self.dashboard_url_prefix,
                    debug=False
                )
                
                if self.dashboard_thread:
                    logger.info(f"Web dashboard started successfully on http://0.0.0.0:{self.dashboard_port}{self.dashboard_url_prefix}/")
                else:
                    logger.error("Failed to start dashboard thread")
                    self.dashboard_enabled = False
            except Exception as e:
                logger.error(f"Failed to start dashboard: {e}", exc_info=True)
                self.dashboard_enabled = False
        
        # Track voice reconnection attempts to prevent infinite reconnection loops
        self.reconnection_attempts = {}
        self.MAX_RECONNECTION_ATTEMPTS = 3
            
        await self.change_presence(activity=discord.Game(name="your dog music fr"))
        self.presence_loop.start()
        self.auto_leave_check.start() # Start the new auto-leave task
        
    async def on_voice_state_update(self, member, before, after):
        """Handle voice state changes, including reconnection when disconnected."""
        try:
            # Only care about our own voice state changes
            if member.id != self.user.id:
                return
                
            # If we were in a voice channel but now we're not, it might be an unexpected disconnection
            if before.channel and not after.channel:
                guild_id = before.channel.guild.id
                guild = before.channel.guild
                
                # Check if we should be in voice (queue not empty or active downloads)
                should_reconnect = False
                try:
                    if guild_id in self.queue_manager.current_songs:
                        should_reconnect = True
                    elif self.queue_manager.playback_queues.get(guild_id, []):
                        should_reconnect = True
                    elif self.queue_manager.active_downloads.get(guild_id, {}):
                        should_reconnect = True
                except Exception as e:
                    logger.error(f"Error checking if reconnection is needed: {e}")
                
                # Check if we've tried to reconnect too many times
                current_time = time.time()
                if should_reconnect:
                    # Initialize or update reconnection attempts
                    if guild_id not in self.reconnection_attempts:
                        self.reconnection_attempts[guild_id] = {'count': 0, 'last_attempt': 0}
                    
                    # Reset counter if last attempt was more than 5 minutes ago
                    if current_time - self.reconnection_attempts[guild_id]['last_attempt'] > 300:
                        self.reconnection_attempts[guild_id]['count'] = 0
                    
                    # Increment counter and update timestamp
                    self.reconnection_attempts[guild_id]['count'] += 1
                    self.reconnection_attempts[guild_id]['last_attempt'] = current_time
                    
                    # Attempt reconnection if we haven't tried too many times
                    if self.reconnection_attempts[guild_id]['count'] <= self.MAX_RECONNECTION_ATTEMPTS:
                        logger.info(f"Unexpected disconnection detected. Attempting to reconnect to {before.channel.name} (attempt {self.reconnection_attempts[guild_id]['count']})...")
                        try:
                            # Try to reconnect to the same channel
                            await before.channel.connect(timeout=10.0, reconnect=True)
                            logger.info(f"Successfully reconnected to {before.channel.name}")
                            
                            # Resume playback if there was a current song
                            if guild_id in self.queue_manager.current_songs:
                                logger.info(f"Resuming playback after reconnection")
                                await self._play_next(guild, None)  # Use None for interaction as this is automatic
                        except Exception as e:
                            logger.error(f"Failed to reconnect to voice channel: {e}")
                    else:
                        logger.warning(f"Reached maximum reconnection attempts ({self.MAX_RECONNECTION_ATTEMPTS}) for guild {guild_id}. Will not attempt further reconnections.")
                        # Clear the current song and queue to prevent further reconnection attempts
                        if guild_id in self.queue_manager.current_songs:
                            current_song = self.queue_manager.current_songs.pop(guild_id, None)
                            if current_song:
                                await self.queue_manager.cleanup_file(current_song.filename)
                        # Reset reconnection counter after giving up
                        self.reconnection_attempts[guild_id]['count'] = 0
                        
            # Update dashboard stats after voice state change
            update_stats()
        except Exception as e:
            logger.error(f"Error in voice_state_update handler: {e}", exc_info=True)

    async def setup_commands(self) -> None:
        """Register all music-related commands"""
        
        @self.tree.command(name="play", description="Play a song from URL")
        async def play(interaction: discord.Interaction, url: str, page: int = 1):
            await interaction.response.defer()
            try:
                voice_client = await self._ensure_voice_client(interaction)
                if not voice_client:
                    await interaction.followup.send("Failed to join voice channel.")
                    return

                # Send initial processing message
                initial_embed = discord.Embed(
                    title="Processing Request",
                    description=f"Attempting to add to download queue: {url}",
                    color=discord.Color.blue()
                )
                # Defer might have already been called, so try to send followup, or edit if a message exists
                try:
                    # Check if interaction is already responded to by defer()
                    if not interaction.response.is_done():
                         await interaction.response.send_message(embed=initial_embed, ephemeral=True)
                    else: # If deferred, send a followup.
                         await interaction.followup.send(embed=initial_embed, ephemeral=True) # Keep it ephemeral for now
                except discord.errors.InteractionResponded:
                     # If it's already responded in some other way, log it or try channel send
                     logger.warning("Play command interaction already responded before initial message.")
                     await interaction.channel.send(embed=initial_embed)

                # Check if it's a Spotify URL
                if self.spotify_client.is_available() and self.spotify_client.is_spotify_url(url):
                    logger.info(f"Detected Spotify URL: {url}")
                    # _handle_spotify_url will now use queue_manager.submit_for_download
                    spotify_handled = await self._handle_spotify_url(url, interaction, page)
                    if not spotify_handled: # e.g. invalid spotify URL structure after check
                        await interaction.followup.send("Failed to process Spotify URL. Please check the URL.", ephemeral=True)
                    return # Spotify handler will manage followup messages

                # For direct YouTube URLs or search terms
                await self.queue_manager.submit_for_download(interaction.guild_id, url, interaction)
                # The confirmation message "Track Added to Queue" will be sent by _add_to_playback_queue
                # or a "Download failed" message by _process_download_pipeline.
                # The initial_embed serves as "working on it".

            except Exception as e:
                logger.error(f"Error in play command: {e}", exc_info=True)
                if not interaction.response.is_done():
                    await interaction.response.send_message("Failed to process your request.", ephemeral=True)
                else:
                    try:
                        await interaction.followup.send("Failed to process your request.", ephemeral=True)
                    except discord.errors.NotFound:
                        logger.error("Interaction not found for error in play command.")

        @self.tree.command(name="skip", description="Skip the current song")
        async def skip(interaction: discord.Interaction):
            await interaction.response.defer()
            guild_name = interaction.guild.name
            
            if not interaction.guild.voice_client or not interaction.guild.voice_client.is_playing():
                logger.info(f"Skip command failed - nothing playing in guild: {guild_name}")
                await interaction.followup.send("Nothing is playing right now!")
                return

            # Stop the current song
            logger.info(f"Skipping current song in guild: {guild_name}")
            interaction.guild.voice_client.stop()
            
            current_song = self.queue_manager.current_songs.get(interaction.guild_id)
            if current_song:
                logger.info(f"Skipped song in guild {guild_name}: {current_song.title}")
                await interaction.followup.send(f"⏭️ Skipped: {current_song.title}")
            else:
                await interaction.followup.send("⏭️ Skipped current song")

        @self.tree.command(name="queue", description="Show the music queue")
        async def queue(interaction: discord.Interaction):
            # Defer the response to prevent timeout during data gathering
            await interaction.response.defer(ephemeral=False)
            
            embed = discord.Embed(title="Music Queue", color=discord.Color.purple())
            
            try:
                current_song = self.queue_manager.current_songs.get(interaction.guild_id)
                if current_song:
                    embed.add_field(
                        name="Now Playing",
                        value=f"[{current_song.title}]({current_song.url})",
                        inline=False
                    )
                else:
                    embed.add_field(
                        name="Now Playing",
                        value="Nothing is currently playing.",
                        inline=False
                    )

                queue_list = []
                # Show playback_queues (songs ready to play) with proper error handling
                try:
                    async with self.queue_manager._queue_locks[interaction.guild_id]:
                        for idx, song in enumerate(self.queue_manager.playback_queues.get(interaction.guild_id, []), 1):
                            queue_list.append(f"{idx}. [{song.title}]({song.url})")
                except Exception as e:
                    logger.error(f"Error accessing queue data: {e}")
                    queue_list = ["Error accessing queue data"]

                if queue_list:
                    embed.add_field(
                        name="Up Next (Ready to Play)",
                        value="\n".join(queue_list),
                        inline=False
                    )
                else:
                    embed.add_field(
                        name="Up Next (Ready to Play)",
                        value="The playback queue is empty.",
                        inline=False
                    )

                # Optionally, show songs in download pipeline with better error handling
                downloading_list = []
                try:
                    # Use a short timeout for the lock to prevent hanging
                    async with asyncio.timeout(1.0):
                        async with self.queue_manager._pipeline_locks[interaction.guild_id]:
                            # Get active downloads
                            active_dl_urls = list(self.queue_manager.active_downloads.get(interaction.guild_id, {}).keys())
                            for item_url in active_dl_urls:
                                downloading_list.append(f"- 🔄 **Downloading:** {item_url[:60]}{'...' if len(item_url)>60 else ''}")
                            
                            # Try to get pending downloads safely
                            pipeline = self.queue_manager.download_pipelines.get(interaction.guild_id)
                            if pipeline and not pipeline.empty():
                                try:
                                    # Create a copy of queue items without consuming them
                                    temp_pipeline_items = list(pipeline._queue)
                                    for item_url, _, _, _ in temp_pipeline_items[:5]:  # Limit to 5 pending items
                                        downloading_list.append(f"- ⏳ **Queued:** {item_url[:50]}{'...' if len(item_url)>50 else ''}")
                                except Exception as e:
                                    logger.warning(f"Could not access download queue items: {e}")
                except asyncio.TimeoutError:
                    downloading_list.append("- (Queue data unavailable - system busy)")
                except Exception as e:
                    logger.error(f"Error accessing download pipeline: {e}")
                
                if downloading_list:
                    embed.add_field(
                        name="Download Pipeline",
                        value="\n".join(downloading_list),
                        inline=False
                    )
                
                await interaction.followup.send(embed=embed)
                
            except Exception as e:
                logger.error(f"Error in queue command: {e}", exc_info=True)
                await interaction.followup.send("Error retrieving queue information. Please try again later.", ephemeral=True)

        @self.tree.command(name="clear", description="Clear the queue")
        async def clear(interaction: discord.Interaction):
            # Check both playback and download queues before declaring empty
            is_playback_empty = not self.queue_manager.playback_queues[interaction.guild_id]
            is_download_pipeline_empty = self.queue_manager.download_pipelines[interaction.guild_id].empty()
            is_active_downloads_empty = not self.queue_manager.active_downloads[interaction.guild_id]

            if is_playback_empty and is_download_pipeline_empty and is_active_downloads_empty:
                await interaction.response.send_message("The queue and download pipeline are already empty!")
                return

            await self.queue_manager.clear_guild_queue(interaction.guild_id)
            await interaction.response.send_message("🗑️ Cleared the music queue and download pipeline!")

        @self.tree.command(name="pause", description="Pause the current song")
        async def pause(interaction: discord.Interaction):
            if not interaction.guild.voice_client or not interaction.guild.voice_client.is_playing():
                await interaction.response.send_message("Nothing is playing!")
                return

            if interaction.guild.voice_client.is_paused():
                await interaction.response.send_message("Already paused!")
                return

            interaction.guild.voice_client.pause()
            await interaction.response.send_message("⏸️ Paused")

        @self.tree.command(name="resume", description="Resume the song")
        async def resume(interaction: discord.Interaction):
            if not interaction.guild.voice_client:
                await interaction.response.send_message("I'm not in a voice channel!")
                return

            if not interaction.guild.voice_client.is_paused():
                await interaction.response.send_message("Not paused!")
                return

            interaction.guild.voice_client.resume()
            await interaction.response.send_message("▶️ Resumed")
            
        @self.tree.command(name="setcookies", description="Update YouTube cookies for authenticated playback")
        async def setcookies(interaction: discord.Interaction, cookie_data: str):
            # Only server admins can update cookies
            if not interaction.user.guild_permissions.administrator:
                await interaction.response.send_message("❌ Only server administrators can update YouTube cookies!", ephemeral=True)
                return
            
            await interaction.response.defer(ephemeral=True)
            
            try:
                # Validate that the cookie data looks like Netscape format
                if not cookie_data.startswith("# Netscape HTTP Cookie File") and not cookie_data.startswith("# http"):
                    await interaction.followup.send(
                        "❌ Invalid cookie format! Please provide cookies in Netscape format (cookies.txt).\n"
                        "You can use browser extensions like 'Get cookies.txt' for Chrome or 'cookies.txt' for Firefox.", 
                        ephemeral=True
                    )
                    return
                
                # Write the new cookies to file
                with open(YOUTUBE_COOKIES, 'w', encoding='utf-8') as f:
                    f.write(cookie_data)
                
                logger.info(f"YouTube cookies updated by admin {interaction.user.name} in guild {interaction.guild.name}")
                await interaction.followup.send("✅ YouTube cookies updated successfully!", ephemeral=True)
                
            except Exception as e:
                logger.error(f"Error updating YouTube cookies: {e}")
                await interaction.followup.send(
                    f"❌ Failed to update YouTube cookies: {str(e)}\n"
                    "Please try again later.", 
                    ephemeral=True
                )

    async def _ensure_voice_client(self, interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
        if not interaction.user.voice:
            # Check if interaction already responded
            if not interaction.response.is_done():
                await interaction.response.send_message("You must be in a voice channel to use this command!", ephemeral=True)
            else:
                await interaction.followup.send("You must be in a voice channel to use this command!", ephemeral=True)
            return None

        voice_channel = interaction.user.voice.channel
        voice_client = interaction.guild.voice_client

        if voice_client:
            if voice_client.channel != voice_channel:
                await voice_client.move_to(voice_channel)
        else:
            try:
                voice_client = await voice_channel.connect()
            except Exception as e:
                logger.error(f"Failed to connect to voice channel: {e}")
                return None

        return voice_client

    async def _play_next(self, guild: discord.Guild, interaction: discord.Interaction) -> None:
        try:
            song = await self.queue_manager.get_next_song(guild.id) # Gets from playback_queues
            if not song:
                if guild.voice_client:
                    await self._play_leave_sound(guild.voice_client)
                # Ensure download worker is started if there are pending downloads, even if playback queue is empty now
                await self.queue_manager._ensure_download_worker_running(guild.id)
                return

            # Verify the song file exists before playing
            if not os.path.exists(song.filename):
                logger.error(f"Song file missing: {song.filename}")
                await interaction.channel.send(f"⚠️ Error: Could not play {song.title} (file missing)")
                # Try to play next song
                await self._play_next(guild, interaction)
                return

            self.queue_manager.current_songs[guild.id] = song
            await self.queue_manager.remove_song(guild.id, 0)
            # Update dashboard state to reflect the new song playing
            update_stats()

            try:
                audio_source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(song.filename),
                    volume=DEFAULT_VOLUME
                )
                
                guild.voice_client.play(
                    audio_source,
                    after=lambda e: asyncio.run_coroutine_threadsafe(
                        self._after_play(e, interaction, song),
                        self.loop
                    )
                )
                
                # Record song play in dashboard
                if self.dashboard_enabled:
                    try:
                        record_song_played(guild.id, song)
                    except Exception as e:
                        logger.error(f"Failed to record song in dashboard: {e}")
                
                await self._send_now_playing_embed(interaction, song)

            except Exception as e:
                logger.error(f"Error starting playback: {e}")
                await interaction.channel.send(f"⚠️ Error playing {song.title}")
                # Clean up the failed song and try next
                await self.queue_manager.cleanup_file(song.filename)
                await self._play_next(guild, interaction)
                # Ensure download worker is started if there are pending downloads
                await self.queue_manager._ensure_download_worker_running(guild.id)

        except Exception as e:
            logger.error(f"Error in play_next: {e}")
            if interaction and interaction.channel: # Check if interaction and channel exist
                await interaction.channel.send("Failed to play next song.")
            # Ensure download worker is started if there are pending downloads
            await self.queue_manager._ensure_download_worker_running(guild.id)

    async def _play_leave_sound(self, voice_client: discord.VoiceClient) -> None:
        try:
            # Update dashboard state before potentially leaving
            update_stats()
            # Wait for 10 seconds
            await asyncio.sleep(10)
            
            # Check if we're still connected (user didn't manually disconnect us)
            if voice_client.is_connected():
                # Play the leave sound
                leave_source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio("leave.mp3"),
                    volume=0.2  # Set leave sound volume
                )
                
                def after_leave(error):
                    if error:
                        logger.error(f"Error playing leave sound: {error}")
                    # Disconnect after leave sound finishes
                    asyncio.run_coroutine_threadsafe(
                        voice_client.disconnect(),
                        self.loop
                    )
                
                voice_client.play(leave_source, after=after_leave)
        except Exception as e:
            logger.error(f"Error in play_leave_sound: {e}")
            # If there's an error, just disconnect
            await voice_client.disconnect()

    async def _after_play(self, error: Optional[Exception], interaction: discord.Interaction, song: Song) -> None:
        try:
            guild_name = interaction.guild.name
            if error:
                logger.error(f"Playback error in guild {guild_name}: {str(error)}")
                await interaction.channel.send(f"⚠️ Error during playback of {song.title}")

            logger.info(f"Song finished in guild {guild_name}: {song.title}")
            # Schedule cleanup of the finished song
            await self.queue_manager.cleanup_file(song.filename)
            self.queue_manager.current_songs.pop(interaction.guild_id, None)
            # Update dashboard state after song finishes and is removed
            update_stats()

            # Start next song or prepare to leave
            if self.queue_manager.playback_queues[interaction.guild_id]: # Check playback_queues
                logger.info(f"Playing next song in queue for guild: {guild_name}")
                await self._play_next(interaction.guild, interaction)
            elif interaction.guild.voice_client:
                logger.info(f"Queue empty, preparing to leave guild: {guild_name}")
                await self._play_leave_sound(interaction.guild.voice_client)
            
            # After a song finishes, ensure the download worker is active to fill the buffer
            await self.queue_manager._ensure_download_worker_running(interaction.guild_id)

        except Exception as e:
            logger.error(f"Error after playback for guild {interaction.guild.name}: {str(e)}", exc_info=True)
            await interaction.channel.send("Failed to play next song.")

    async def _send_now_playing_embed(self, interaction: discord.Interaction, song: Song) -> None:
        """Send a message with the currently playing song details."""
        embed = discord.Embed(
            title="Now Playing",
            description=f"[{song.title}]({song.url})",
            color=discord.Color.green()
        )
        
        # Only add thumbnail if it's a valid URL
        if song.thumbnail and isinstance(song.thumbnail, str) and (
            song.thumbnail.startswith('http://') or 
            song.thumbnail.startswith('https://')
        ):
            embed.set_thumbnail(url=song.thumbnail)
            
        embed.add_field(name="Duration", value=str(song.duration))
        
        # Add queued by information
        embed.set_footer(text=f"Queued by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)

        # Send as a new message instead of a follow-up
        await interaction.channel.send(embed=embed)

    async def _handle_spotify_url(self, url: str, interaction: discord.Interaction, page: int) -> bool:
        """Handle Spotify URLs (track, playlist, album)."""
        if not self.spotify_client.is_available():
            await interaction.followup.send("Spotify support is not configured. Please set up Spotify API credentials.")
            return False

        track_type = self.spotify_client.get_track_type(url)
        if not track_type:
            return False

        if track_type == "track":
            return await self._handle_spotify_track(url, interaction, page)
        elif track_type == "playlist":
            return await self._handle_spotify_playlist(url, interaction, page)
        elif track_type == "album":
            return await self._handle_spotify_album(url, interaction, page)
        
        return False

    async def _handle_spotify_track(self, url: str, interaction: discord.Interaction, page: int) -> bool:
        """Handle a single Spotify track."""
        track_id = self.spotify_client.get_track_id(url)
        if not track_id:
            embed = discord.Embed(
                title="Invalid Track",
                description="Could not extract Spotify track ID.",
                color=discord.Color.red()
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)
            return False

        # Send processing message - this is now handled by the general play command or the worker
        # processing_embed = discord.Embed(
        #     title="Processing Track",
        #     description="Adding Spotify track to download queue...",
        #     color=discord.Color.blue()
        # )
        # processing_message = await interaction.followup.send(embed=processing_embed)
        
        # Submit to download queue
        await self.queue_manager.submit_for_download(interaction.guild_id, url, interaction, is_spotify=True)
        # Confirmation will be sent by the download worker upon successful download and queueing for playback.
        # The initial "Processing Request" message from /play should suffice for now.
        
        return True

    async def _handle_spotify_playlist(self, url: str, interaction: discord.Interaction, page: int) -> bool:
        """Handle a Spotify playlist."""
        playlist_id = self.spotify_client.get_playlist_id(url)
        if not playlist_id:
            embed = discord.Embed(
                title="Invalid Playlist",
                description="Could not extract Spotify playlist ID.",
                color=discord.Color.red()
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)
            return False

        # Get basic playlist info for the name
        try:
            playlist_info = self.spotify_client.client.playlist(playlist_id, fields="name,tracks(total)")
            playlist_name = playlist_info.get('name', 'Unknown Playlist')
            playlist_total = playlist_info.get('tracks', {}).get('total', 0)
        except Exception as e:
            logger.error(f"Error getting playlist info: {e}")
            playlist_name = 'Spotify Playlist'
            playlist_total = 0

        # Limit to 100 tracks per page
        max_tracks = 100
        start_track = (page - 1) * max_tracks + 1
        
        # Calculate total number of pages
        total_tracks = len(await self.spotify_client.get_playlist_tracks(playlist_id))
        max_pages = (total_tracks + max_tracks - 1) // max_tracks  # Ceiling division
        
        # Get all tracks metadata first
        playlist_tracks = await self.spotify_client.get_playlist_tracks(playlist_id)
        if not playlist_tracks:
            embed = discord.Embed(
                title="No Tracks Found",
                description=f"No tracks found in playlist.",
                color=discord.Color.red()
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)
            return False
            
        # Apply pagination
        start_idx = (page - 1) * max_tracks
        end_idx = min(start_idx + max_tracks, len(playlist_tracks))
        playlist_tracks_page = playlist_tracks[start_idx:end_idx]
        
        if not playlist_tracks_page:
            embed = discord.Embed(
                title="No Tracks Found",
                description=f"Page {page} has no tracks.",
                color=discord.Color.red()
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)
            return False
        
        # Send initial progress embed
        # The /play command now sends an initial "Processing Request" message.
        # This detailed playlist processing message can be a followup.
        status_embed = discord.Embed(
            title=f"Playlist: {playlist_name}",
            description=f"Found {len(playlist_tracks_page)} tracks on page {page}. Adding to download queue...",
            color=discord.Color.blue()
        )
        # Add queued by information
        status_embed.set_footer(text=f"Queued by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
            
        # Ensure this followup is valid
        status_message = None
        if interaction.response.is_done():
            status_message = await interaction.followup.send(embed=status_embed)
        else:
            # This case should ideally not happen if /play deferred or sent initial message.
            # Log or handle error if interaction cannot be used.
            logger.warning("Interaction not ready for followup in _handle_spotify_playlist")
            await interaction.response.send_message(embed=status_embed) # Attempt to send if not responded
            # Re-fetch might be needed if send_message was used. This part is tricky.
            # For simplicity, assume followup.send is the primary path after initial /play message.

        # Process tracks one by one, adding to download queue
        added_to_pipeline_count = 0
        failed_count = 0
        
        for i, track_data in enumerate(playlist_tracks_page):
            if not track_data:
                failed_count +=1
                continue
                
            try:
                track_id = track_data.get('id')
                if not track_id:
                    failed_count +=1
                    continue
                
                track_url = f"https://open.spotify.com/track/{track_id}"
                # Pass necessary track info for potential direct use by Spotify download logic if needed
                spotify_item_info = self.spotify_client.SpotifyItem(track_data) if hasattr(self.spotify_client, 'SpotifyItem') else track_data

                await self.queue_manager.submit_for_download(
                    interaction.guild_id, 
                    track_url, 
                    interaction, # Pass interaction for potential error messages from worker
                    is_spotify=True, 
                    spotify_info=spotify_item_info
                )
                added_to_pipeline_count += 1
                
                if (i + 1) % 10 == 0 and status_message: # Update status message periodically
                    status_embed.description = f"Added {added_to_pipeline_count}/{len(playlist_tracks_page)} tracks to download queue..."
                    await status_message.edit(embed=status_embed)

            except Exception as e:
                logger.error(f"Error submitting playlist track to download queue: {e}")
                failed_count +=1
                # Continue with next track

        # Final update with completion status
        if status_message:
            if added_to_pipeline_count > 0:
                status_embed.title = f"Playlist: {playlist_name} - Processing"
                status_embed.description = f"Successfully submitted {added_to_pipeline_count}/{len(playlist_tracks_page)} tracks to the download queue."
                if failed_count > 0:
                    status_embed.description += f" ({failed_count} tracks failed to submit)."
                status_embed.color = discord.Color.green()
                
                if page < max_pages:
                    status_embed.add_field(
                        name="More Tracks", 
                        value=f"Use `/play {url} page:{page+1}` for the next page.",
                        inline=False
                    )
            else:
                status_embed.title = f"Playlist Processing Failed"
                status_embed.description = f"Could not submit any tracks from the playlist to the download queue."
                if failed_count > 0:
                    status_embed.description += f" ({failed_count} tracks failed during submission attempt)."
                status_embed.color = discord.Color.red()
            await status_message.edit(embed=status_embed)
                
        return added_to_pipeline_count > 0

    async def _handle_spotify_album(self, url: str, interaction: discord.Interaction, page: int) -> bool:
        """Handle a Spotify album."""
        album_id = self.spotify_client.get_album_id(url)
        if not album_id:
            embed = discord.Embed(
                title="Invalid Album",
                description="Could not extract Spotify album ID.",
                color=discord.Color.red()
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)
            return False

        # Get basic album info for the name
        try:
            album_info = self.spotify_client.client.album(album_id)
            album_name = album_info.get('name', 'Unknown Album')
            album_artist = album_info.get('artists', [{}])[0].get('name', 'Unknown Artist')
            album_display = f"{album_artist} - {album_name}"
            album_total = album_info.get('total_tracks', 0)
            album_image = album_info.get('images', [{}])[0].get('url') if album_info.get('images') else None
        except Exception as e:
            logger.error(f"Error getting album info: {e}")
            album_display = 'Spotify Album'
            album_total = 0
            album_image = None

        # Limit to 100 tracks per page
        max_tracks = 100
        start_track = (page - 1) * max_tracks + 1
        
        # Calculate total number of pages
        total_tracks = len(await self.spotify_client.get_album_tracks(album_id))
        max_pages = (total_tracks + max_tracks - 1) // max_tracks  # Ceiling division
        
        # Get all album tracks first
        album_tracks = await self.spotify_client.get_album_tracks(album_id)
        if not album_tracks:
            embed = discord.Embed(
                title="No Tracks Found",
                description=f"No tracks found in album.",
                color=discord.Color.red()
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)
            return False
            
        # Apply pagination
        start_idx = (page - 1) * max_tracks
        end_idx = min(start_idx + max_tracks, len(album_tracks))
        album_tracks_page = album_tracks[start_idx:end_idx]
        
        if not album_tracks_page:
            embed = discord.Embed(
                title="No Tracks Found",
                description=f"Page {page} has no tracks.",
                color=discord.Color.red()
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)
            return False
        
        # Send initial progress embed
        status_embed = discord.Embed(
            title=f"Album: {album_display}",
            description=f"Found {len(album_tracks_page)} tracks on page {page}. Adding to download queue...",
            color=discord.Color.blue()
        )
        if album_image:
            status_embed.set_thumbnail(url=album_image)
        
        # Add queued by information
        status_embed.set_footer(text=f"Queued by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
            
        status_message = None
        if interaction.response.is_done():
            status_message = await interaction.followup.send(embed=status_embed)
        else:
            logger.warning("Interaction not ready for followup in _handle_spotify_album")
            await interaction.response.send_message(embed=status_embed) # Fallback

        
        # Process tracks one by one, adding to download queue
        added_to_pipeline_count = 0
        failed_count = 0
        
        for i, track_data in enumerate(album_tracks_page):
            if not track_data:
                failed_count += 1
                continue
                
            try:
                track_id = track_data.get('id')
                if not track_id:
                    failed_count +=1
                    continue
                    
                track_url = f"https://open.spotify.com/track/{track_id}"
                # Pass necessary track info for potential direct use by Spotify download logic if needed
                spotify_item_info = self.spotify_client.SpotifyItem(track_data) if hasattr(self.spotify_client, 'SpotifyItem') else track_data

                await self.queue_manager.submit_for_download(
                    interaction.guild_id,
                    track_url,
                    interaction, # Pass interaction for potential error messages from worker
                    is_spotify=True,
                    spotify_info=spotify_item_info # Pass full track data if useful for spotify_client.download_track
                )
                added_to_pipeline_count += 1

                if (i + 1) % 10 == 0 and status_message: # Update status message periodically
                    status_embed.description = f"Added {added_to_pipeline_count}/{len(album_tracks_page)} tracks to download queue..."
                    await status_message.edit(embed=status_embed)
                    
            except Exception as e:
                logger.error(f"Error submitting album track to download queue: {e}")
                failed_count +=1
                # Continue with next track

        # Final update with completion status
        if status_message:
            if added_to_pipeline_count > 0:
                status_embed.title = f"Album: {album_display} - Processing"
                status_embed.description = f"Successfully submitted {added_to_pipeline_count}/{len(album_tracks_page)} tracks to the download queue."
                if failed_count > 0:
                    status_embed.description += f" ({failed_count} tracks failed to submit)."
                status_embed.color = discord.Color.green()
            
                if page < max_pages:
                    status_embed.add_field(
                        name="More Tracks", 
                        value=f"Use `/play {url} page:{page+1}` for the next page.",
                        inline=False
                    )
            else:
                status_embed.title = f"Album Processing Failed"
                status_embed.description = f"Could not submit any tracks from the album to the download queue."
                if failed_count > 0:
                    status_embed.description += f" ({failed_count} tracks failed during submission attempt)."
                status_embed.color = discord.Color.red()
            await status_message.edit(embed=status_embed)
                
        return added_to_pipeline_count > 0

    async def _download_song(self, url: str) -> Optional[Song]:
        """Download a song from YouTube using yt-dlp."""
        try:
            # Create downloads directory if it doesn't exist
            os.makedirs(os.path.dirname(YOUTUBE_COOKIES_WRITABLE), exist_ok=True)
            
            # Determine which cookies file to use
            cookies_file = None
            if os.path.exists(YOUTUBE_COOKIES):
                try:
                    # Make a writable copy of the cookies file
                    shutil.copy2(YOUTUBE_COOKIES, YOUTUBE_COOKIES_WRITABLE)
                    cookies_file = YOUTUBE_COOKIES_WRITABLE
                    logger.info(f"Created writable copy of YouTube cookies at: {cookies_file}")
                except Exception as e:
                    logger.warning(f"Failed to create writable cookies copy: {e}")
            
            # First fetch metadata using yt-dlp
            ydl_opts = {
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'outtmpl': 'downloads/%(title)s.%(ext)s',
                'quiet': False,
                'no_warnings': True,
                'extract_flat': False,
                'noplaylist': True,
                # Only download audio stream
                'format_sort': ['acodec:mp3:128'],
                'postprocessor_args': ['-ar', '44100'],
                'prefer_ffmpeg': True,
                # Add rate limiting to reduce network impact and prevent disconnections
                'ratelimit': 400000,  # 400KB/s - adjust based on your connection
                # Add socket timeout to handle network issues gracefully
                'socket_timeout': 30,
                # Throttle the requests to YouTube API
                'sleep_interval': 2,  # sleep 2 seconds between requests
                'max_sleep_interval': 5,  # maximum sleep time
                # Limit retries on network errors
                'retries': 5,
            }
            
            # Add proxy configuration if set in environment variables
            # This allows the bot to connect through a proxy server for YouTube downloads
            # Useful for environments where direct connections are blocked or rate-limited
            if PROXY_URL:
                logger.info(f"Using proxy for download: {PROXY_URL}")
                ydl_opts['proxy'] = PROXY_URL
            
            # Add cookies file only if it exists
            if cookies_file and os.path.exists(cookies_file):
                logger.info(f"Using YouTube cookies file for download: {cookies_file}")
                ydl_opts['cookiefile'] = cookies_file
            else:
                logger.warning(f"YouTube cookies file not available or not found")
                
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                logger.info(f"Downloading audio from: {url}")
                info = await asyncio.get_event_loop().run_in_executor(
                    ThreadPoolExecutor(1),
                    lambda: ydl.extract_info(url, download=True)
                )
                
                if not info:
                    logger.error("No info returned from yt-dlp")
                    return None

                filename = ydl.prepare_filename(info)
                # Adjust filename for mp3 extension since we're extracting audio
                filename = filename.rsplit(".", 1)[0] + ".mp3"
                
                # Format duration as mm:ss
                duration_sec = info.get('duration', 0)
                if duration_sec:
                    minutes = int(duration_sec // 60)
                    seconds = int(duration_sec % 60)
                    duration_str = f"{minutes}:{seconds:02d}"
                else:
                    duration_str = "Unknown"

                return Song(
                    title=info.get('title', 'Unknown Title'),
                    url=url,
                    filename=filename,
                    thumbnail=info.get('thumbnail'),
                    duration=duration_str
                )

        except Exception as e:
            logger.error(f"Error downloading song with yt-dlp: {str(e)}")
            return None

    @tasks.loop(seconds=30)
    async def presence_loop(self):
        statuses = [
            "HELP ME IM STUCK IN AWS",
            "Im not a bot, I am a human",
            "Lebron lying face down so funny bruh",
            "11.219464, 123.732551"
        ]
        for status in itertools.cycle(statuses):
            await self.change_presence(activity=discord.CustomActivity(name=status))
            await asyncio.sleep(30)  # Sleep for 30 seconds before changing to the next status

    @presence_loop.before_loop
    async def before_presence_loop(self):
        await self.wait_until_ready()

    @tasks.loop(seconds=15) # Check every 15 seconds
    async def auto_leave_check(self):
        if not self.is_ready(): # Don't run until bot is ready
            return

        for vc in self.voice_clients:
            guild_id = vc.guild.id
            
            # Check if anyone non-bot is in the channel
            human_members = [member for member in vc.channel.members if not member.bot]
            
            is_bot_alone = len(human_members) == 0
            is_playing_or_paused = vc.is_playing() or vc.is_paused()
            
            # Check bot's own queues and download state via QueueManager
            # Ensure queue_manager has self.bot set to access its own structures if needed, or pass guild_id
            playback_queue_empty = not self.queue_manager.playback_queues.get(guild_id)
            
            download_pipeline = self.queue_manager.download_pipelines.get(guild_id)
            download_pipeline_empty = download_pipeline.empty() if download_pipeline else True
            
            active_downloads_for_guild = self.queue_manager.active_downloads.get(guild_id)
            no_active_downloads = not active_downloads_for_guild # True if dict is empty or None

            is_bot_idle = not is_playing_or_paused and playback_queue_empty and download_pipeline_empty and no_active_downloads

            if is_bot_alone and is_bot_idle:
                if guild_id not in self.alone_since_timestamps:
                    self.alone_since_timestamps[guild_id] = asyncio.get_event_loop().time()
                    logger.info(f"Bot is alone and idle in VC for guild {guild_id}. Starting 30s auto-leave timer.")
                else:
                    time_alone = asyncio.get_event_loop().time() - self.alone_since_timestamps[guild_id]
                    if time_alone > 30: # 30 seconds threshold
                        logger.info(f"Bot has been alone and idle in guild {guild_id} for {time_alone:.2f}s. Leaving voice channel.")
                        await vc.disconnect() # Disconnect directly
                        # No need to play leave sound here, it's an idle timeout.
                        # _play_leave_sound is for when queue ends.
                        if guild_id in self.alone_since_timestamps: # Remove timestamp after disconnecting
                            del self.alone_since_timestamps[guild_id]
            else:
                # Conditions for auto-leave are not met (someone joined, or bot became active)
                if guild_id in self.alone_since_timestamps:
                    logger.info(f"Conditions for auto-leave no longer met for guild {guild_id}. Resetting timer.")
                    del self.alone_since_timestamps[guild_id]

    @auto_leave_check.before_loop
    async def before_auto_leave_check(self):
        await self.wait_until_ready()

# Bot instantiation
if __name__ == "__main__":
    try:
        if not BOT_TOKEN:
            logger.critical("DISCORD_BOT_TOKEN environment variable not set.")
            exit(1)
            
        logger.info("Starting bot...")
        bot = MusicBot()
        bot.run(BOT_TOKEN)
    except Exception as e:
        logger.exception(f"Fatal error during bot startup: {e}")
        exit(1)

