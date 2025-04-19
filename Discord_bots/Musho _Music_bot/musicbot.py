import asyncio
import json
import logging
import os
import shutil
from collections import defaultdict
from typing import Optional, Tuple, List, Dict, Union
import itertools
import re

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
from dashboard import register_bot, record_song_played, start_dashboard

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
            
            # Set up environment variables for spotify-dlp
            os.environ["SPOTIFY_DLP_CLIENT_ID"] = SPOTIFY_CLIENT_ID
            os.environ["SPOTIFY_DLP_CLIENT_SECRET"] = SPOTIFY_CLIENT_SECRET
            
            logger.info("Spotify client initialized successfully.")

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

    async def download_track(self, url: str) -> Optional[Song]:
        """Download a track from Spotify using spotify-dlp."""
        if not self.is_available():
            return None
            
        try:
            # Create a unique download directory for this track
            download_dir = os.path.join(os.getcwd(), "downloads", "spotify")
            os.makedirs(download_dir, exist_ok=True)
            
            # Get track metadata using spotipy for better details
            track_id = self.get_track_id(url)
            if not track_id:
                logger.error(f"Could not extract track ID from Spotify URL: {url}")
                return None
                
            track_info = self.get_track_info(track_id)
            if not track_info:
                logger.error(f"Could not get track info from Spotify: {url}")
                return None
            
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
            
            output_path = os.path.join(download_dir, f"{safe_filename}.mp3")
            
            # Run spotify-dlp to download the track
            logger.info(f"Downloading track from Spotify: {track_artist} - {track_title}")
            
            # Use spotify-dlp in a subprocess
            cmd = [
                "spotify-dlp",
                url,
                "-o", download_dir,
                "-c", "mp3",
                "-m",  # Include metadata
                "-y"   # Skip confirmation
            ]
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                logger.error(f"spotify-dlp failed: {stderr.decode()}")
                return None
                
            # Find the downloaded file (it might have a slightly different name)
            downloaded_files = []
            for file in os.listdir(download_dir):
                if file.endswith(".mp3") and (track_title.lower() in file.lower() or track_artist.lower() in file.lower()):
                    downloaded_files.append(os.path.join(download_dir, file))
            
            if not downloaded_files:
                logger.error("spotify-dlp did not produce an output file")
                return None
                
            # Use the most recently created file as it's likely the one we just downloaded
            downloaded_files.sort(key=os.path.getmtime, reverse=True)
            actual_output_path = downloaded_files[0]
            
            # Create a Song object with the downloaded track
            return Song(
                filename=actual_output_path,
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
            # Create a unique download directory for this playlist
            download_dir = os.path.join(os.getcwd(), "downloads", "spotify", f"playlist_{playlist_id}")
            os.makedirs(download_dir, exist_ok=True)
            
            # Get playlist info for better details
            playlist_info = self.client.playlist(playlist_id)
            playlist_name = playlist_info['name']
            playlist_total = playlist_info.get('tracks', {}).get('total', 0)
            playlist_url = f"https://open.spotify.com/playlist/{playlist_id}"
            
            # Calculate pagination
            start_index = (page - 1) * max_tracks + 1
            end_index = page * max_tracks
            
            # First get all tracks metadata from the playlist
            all_playlist_tracks = await self.get_playlist_tracks(playlist_id)
            if not all_playlist_tracks:
                logger.error(f"Could not fetch tracks for playlist: {playlist_id}")
                return []
                
            # Apply pagination
            start_idx = (page - 1) * max_tracks
            end_idx = min(start_idx + max_tracks, len(all_playlist_tracks))
            playlist_tracks = all_playlist_tracks[start_idx:end_idx]
            
            if not playlist_tracks:
                logger.error(f"No tracks found for playlist page {page}")
                return []
            
            logger.info(f"Processing {len(playlist_tracks)} tracks from playlist '{playlist_name}' (page {page})")
            
            # Use spotify-dlp to download tracks for this page
            cmd = [
                "spotify-dlp",
                playlist_url,
                "-o", download_dir,
                "-c", "mp3",
                "-m",  # Include metadata
                "-y",  # Skip confirmation
                "-l", f"{start_index}:{end_index}"  # Limit to current page
            ]
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                logger.error(f"spotify-dlp failed: {stderr.decode()}")
                return []
            
            # Map of track IDs to metadata to preserve proper information
            track_metadata = {}
            for track in playlist_tracks:
                if track and 'id' in track:
                    track_metadata[track['id']] = {
                        'name': track.get('name', 'Unknown'),
                        'artist': track.get('artists', [{}])[0].get('name', 'Unknown Artist'),
                        'album': track.get('album', {}).get('name', 'Unknown Album'),
                        'duration_ms': track.get('duration_ms', 0),
                        'image': track.get('album', {}).get('images', [{}])[0].get('url') if track.get('album', {}).get('images') else None
                    }
            
            # Find all downloaded MP3 files
            downloaded_songs = []
            for file in os.listdir(download_dir):
                if file.endswith(".mp3"):
                    file_path = os.path.join(download_dir, file)
                    
                    # Try to match with track metadata using filename
                    matched_track = None
                    file_name_lower = file.lower().replace(".mp3", "")
                    
                    for track_id, metadata in track_metadata.items():
                        track_name_lower = metadata['name'].lower()
                        artist_name_lower = metadata['artist'].lower()
                        
                        # Check if track name and artist are in the filename
                        if track_name_lower in file_name_lower and artist_name_lower in file_name_lower:
                            matched_track = metadata
                            # Remove this track from the map to avoid duplicates
                            track_metadata.pop(track_id, None)
                            break
                    
                    # If we found a match, use its metadata
                    if matched_track:
                        duration_ms = matched_track.get('duration_ms', 0)
                        minutes = int((duration_ms / 1000) // 60)
                        seconds = int((duration_ms / 1000) % 60)
                        duration_str = f"{minutes}:{seconds:02d}"
                        
                        song = Song(
                            filename=file_path,
                            title=f"{matched_track['artist']} - {matched_track['name']}",
                            duration=duration_str,
                            url=playlist_url,
                            thumbnail=matched_track.get('image')
                        )
                    else:
                        # Fall back to parsing filename if no match
                        parts = file.replace(".mp3", "").split(" - ", 1)
                        if len(parts) == 2:
                            artist, title = parts
                        else:
                            artist, title = "Unknown Artist", parts[0]
                        
                        song = Song(
                            filename=file_path,
                            title=f"{artist} - {title}",
                            duration="Unknown",
                            url=playlist_url,
                            thumbnail=None
                        )
                    
                    downloaded_songs.append(song)
            
            logger.info(f"Downloaded {len(downloaded_songs)} songs from playlist: {playlist_name}")
            
            # Add playlist info to return object
            for song in downloaded_songs:
                song.playlist_info = {
                    'name': playlist_name,
                    'total_tracks': playlist_total,
                    'current_page': page,
                    'tracks_per_page': max_tracks
                }
                
            return downloaded_songs
            
        except Exception as e:
            logger.error(f"Error downloading playlist from Spotify: {e}", exc_info=True)
            return []
            
    async def download_album(self, album_id: str, max_tracks: int = 100, page: int = 1) -> List[Song]:
        """Download an album from Spotify with pagination support."""
        if not self.is_available():
            return []
            
        try:
            # Create a unique download directory for this album
            download_dir = os.path.join(os.getcwd(), "downloads", "spotify", f"album_{album_id}")
            os.makedirs(download_dir, exist_ok=True)
            
            # Get album info for better details
            album_info = self.client.album(album_id)
            album_name = album_info['name']
            album_artist = album_info['artists'][0]['name']
            album_total = album_info.get('total_tracks', 0)
            
            album_url = f"https://open.spotify.com/album/{album_id}"
            
            # Calculate pagination
            start_index = (page - 1) * max_tracks + 1
            end_index = page * max_tracks
            
            # First get all tracks metadata from the album
            all_album_tracks = await self.get_album_tracks(album_id)
            if not all_album_tracks:
                logger.error(f"Could not fetch tracks for album: {album_id}")
                return []
                
            # Apply pagination
            start_idx = (page - 1) * max_tracks
            end_idx = min(start_idx + max_tracks, len(all_album_tracks))
            album_tracks = all_album_tracks[start_idx:end_idx]
            
            if not album_tracks:
                logger.error(f"No tracks found for album page {page}")
                return []
            
            logger.info(f"Processing {len(album_tracks)} tracks from album '{album_name}' (page {page})")
            
            # Use spotify-dlp to download tracks for this page
            cmd = [
                "spotify-dlp",
                album_url,
                "-o", download_dir,
                "-c", "mp3",
                "-m",  # Include metadata
                "-y",  # Skip confirmation
                "-l", f"{start_index}:{end_index}"  # Limit to current page
            ]
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                logger.error(f"spotify-dlp failed: {stderr.decode()}")
                return []
                
            # Get album images for thumbnail
            album_image = None
            if album_info.get('images') and len(album_info['images']) > 0:
                album_image = album_info['images'][0].get('url')
                
            # Map of track positions to metadata to preserve proper information
            track_metadata = {}
            for track in album_tracks:
                if track:
                    # Use track position as key in an album (more reliable than trying to match by name)
                    position = track.get('track_number', 0)
                    track_metadata[position] = {
                        'name': track.get('name', 'Unknown'),
                        'artist': album_artist,  # Use album artist for consistency
                        'album': album_name,
                        'duration_ms': track.get('duration_ms', 0),
                        'disc_number': track.get('disc_number', 1),
                        'track_number': position,
                        'image': album_image
                    }
            
            # Find all downloaded MP3 files
            downloaded_songs = []
            for file in os.listdir(download_dir):
                if file.endswith(".mp3"):
                    file_path = os.path.join(download_dir, file)
                    
                    # Try to match with track metadata using filename
                    matched_track = None
                    file_name_lower = file.lower().replace(".mp3", "")
                    
                    # First try to match directly with track metadata
                    for position, metadata in track_metadata.items():
                        track_name_lower = metadata['name'].lower()
                        
                        # Check if track name is in the filename (artist should always match in an album)
                        if track_name_lower in file_name_lower:
                            matched_track = metadata
                            # Remove this track from the map to avoid duplicates
                            track_metadata.pop(position, None)
                            break
                    
                    # If we found a match, use its metadata
                    if matched_track:
                        duration_ms = matched_track.get('duration_ms', 0)
                        minutes = int((duration_ms / 1000) // 60)
                        seconds = int((duration_ms / 1000) % 60)
                        duration_str = f"{minutes}:{seconds:02d}"
                        
                        song = Song(
                            filename=file_path,
                            title=f"{matched_track['artist']} - {matched_track['name']}",
                            duration=duration_str,
                            url=album_url,
                            thumbnail=matched_track.get('image')
                        )
                    else:
                        # Fall back to parsing filename if no match
                        parts = file.replace(".mp3", "").split(" - ", 1)
                        if len(parts) == 2:
                            artist, title = parts
                        else:
                            artist, title = album_artist, parts[0]
                        
                        song = Song(
                            filename=file_path,
                            title=f"{artist} - {title}",
                            duration="Unknown",
                            url=album_url,
                            thumbnail=album_image  # At least use the album image
                        )
                    
                    downloaded_songs.append(song)
            
            logger.info(f"Downloaded {len(downloaded_songs)} songs from album: {album_artist} - {album_name}")
            
            # Add album info to return object
            for song in downloaded_songs:
                song.playlist_info = {
                    'name': f"{album_artist} - {album_name}",
                    'total_tracks': album_total,
                    'current_page': page,
                    'tracks_per_page': max_tracks,
                    'is_album': True
                }
                
            return downloaded_songs
            
        except Exception as e:
            logger.error(f"Error downloading album from Spotify: {e}", exc_info=True)
            return []

class QueueManager:
    def __init__(self):
        self.queues = defaultdict(list)
        self.current_songs = {}
        self.file_use_count = defaultdict(int)
        self._queue_locks = defaultdict(asyncio.Lock)
        self._download_tasks = {}  # Track download tasks per guild
        self.download_queue = {}   # Track songs pending download
        self._cleanup_tasks = set()  # Track cleanup tasks

    async def add_song(self, guild_id: int, song: Song) -> None:
        async with self._queue_locks[guild_id]:
            self.queues[guild_id].append(song)
            self.file_use_count[song.filename] += 1
            # Start pre-downloading next songs if needed
            await self._schedule_downloads(guild_id)

    async def remove_song(self, guild_id: int, index: int) -> Optional[Song]:
        async with self._queue_locks[guild_id]:
            if not self.queues[guild_id]:
                return None
            song = self.queues[guild_id].pop(index)
            # Cancel any pending download for this song
            if guild_id in self._download_tasks:
                self._download_tasks[guild_id] = [
                    task for task in self._download_tasks[guild_id]
                    if task.get_name() != song.filename
                ]
            return song

    async def clear_guild_queue(self, guild_id: int) -> None:
        async with self._queue_locks[guild_id]:
            await self._cleanup_guild_resources(guild_id)
            self.queues[guild_id].clear()
            self.current_songs.pop(guild_id, None)
            # Cancel all pending downloads
            if guild_id in self._download_tasks:
                for task in self._download_tasks[guild_id]:
                    task.cancel()
                self._download_tasks[guild_id] = []

    async def _schedule_downloads(self, guild_id: int) -> None:
        """Schedule pre-downloads for upcoming songs"""
        if guild_id not in self._download_tasks:
            self._download_tasks[guild_id] = []

        # Clean up completed download tasks
        self._download_tasks[guild_id] = [
            task for task in self._download_tasks[guild_id]
            if not task.done()
        ]

        # Schedule downloads for the next few songs that haven't been downloaded
        for song in self.queues[guild_id][:3]:  # Pre-download next 3 songs
            if not os.path.exists(song.filename):
                # Check if download is already scheduled
                if not any(task.get_name() == song.filename for task in self._download_tasks[guild_id]):
                    task = asyncio.create_task(
                        self._download_song(song.url),
                        name=song.filename
                    )
                    self._download_tasks[guild_id].append(task)

    async def _cleanup_guild_resources(self, guild_id: int) -> None:
        """Clean up all resources for a guild"""
        cleanup_tasks = []
        for song in self.queues[guild_id]:
            cleanup_tasks.append(self.cleanup_file(song.filename))
        
        # Wait for all cleanup tasks to complete
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)

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
        
        super().__init__(command_prefix='/', intents=intents)
        self.queue_manager = QueueManager()
        self.spotify_client = SpotifyClient()
        self.tree.on_error = self.on_tree_error
        
        # Dashboard settings
        self.dashboard_enabled = True
        self.dashboard_port = int(os.getenv("DASHBOARD_PORT", "80"))
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
                # Register this bot instance with the dashboard
                register_bot(self)
                
                # Start dashboard in background
                self.dashboard_thread = start_dashboard(
                    host='0.0.0.0', 
                    port=self.dashboard_port,
                    url_prefix=self.dashboard_url_prefix,
                    debug=False
                )
                logger.info(f"Web dashboard started on http://localhost:{self.dashboard_port}{self.dashboard_url_prefix}/")
            except Exception as e:
                logger.error(f"Failed to start dashboard: {e}")
                self.dashboard_enabled = False
            
        await self.change_presence(activity=discord.Game(name="your dog music fr"))
        self.presence_loop.start()

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

                # Check if it's a Spotify URL and handle it specially
                if self.spotify_client.is_available() and self.spotify_client.is_spotify_url(url):
                    logger.info(f"Detected Spotify URL: {url}")
                    spotify_handled = await self._handle_spotify_url(url, interaction, page)
                    if spotify_handled:
                        return
                    else:
                        await interaction.followup.send("Failed to process Spotify URL. Please check your Spotify configuration.")
                        return  # Don't attempt to process Spotify URLs with yt-dlp

                # Send initial processing message
                processing_embed = discord.Embed(
                    title="Processing Track",
                    description=f"Downloading from YouTube: {url}",
                    color=discord.Color.blue()
                )
                processing_message = await interaction.followup.send(embed=processing_embed)

                # Download the song
                song = await self._download_song(url)
                if not song:
                    error_embed = discord.Embed(
                        title="Download Failed",
                        description="Failed to download the song.",
                        color=discord.Color.red()
                    )
                    await processing_message.edit(embed=error_embed)
                    return

                await self.queue_manager.add_song(interaction.guild_id, song)
                
                if not voice_client.is_playing():
                    await self._play_next(interaction.guild, interaction)
                else:
                    # Update the processing message with queue info
                    position = len(self.queue_manager.queues[interaction.guild_id])
                    success_embed = discord.Embed(
                        title="Track Added",
                        description=f"Added to queue (Position: {position}): {song.title}",
                        color=discord.Color.green()
                    )
                    if song.thumbnail:
                        success_embed.set_thumbnail(url=song.thumbnail)
                    success_embed.add_field(name="Duration", value=song.duration)
                    await processing_message.edit(embed=success_embed)

            except Exception as e:
                logger.error(f"Error in play command: {e}")
                await interaction.followup.send("Failed to play the song.")

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
            embed = discord.Embed(title="Music Queue", color=discord.Color.purple())
            
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
            for idx, song in enumerate(self.queue_manager.queues[interaction.guild_id], 1):
                queue_list.append(f"{idx}. [{song.title}]({song.url})")

            if queue_list:
                embed.add_field(
                    name="Up Next",
                    value="\n".join(queue_list),
                    inline=False
                )
            else:
                embed.add_field(
                    name="Up Next",
                    value="The queue is empty.",
                    inline=False
                )

            await interaction.response.send_message(embed=embed)



        @self.tree.command(name="clear", description="Clear the queue")
        async def clear(interaction: discord.Interaction):
            if not self.queue_manager.queues[interaction.guild_id]:
                await interaction.response.send_message("The queue is already empty!")
                return

            await self.queue_manager.clear_guild_queue(interaction.guild_id)
            await interaction.response.send_message("🗑️ Cleared the music queue!")

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
            await interaction.followup.send("You must be in a voice channel to use this command!")
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
            if not guild.id in self.queue_manager.queues or not self.queue_manager.queues[guild.id]:
                if guild.voice_client:
                    await self._play_leave_sound(guild.voice_client)
                return

            song = self.queue_manager.queues[guild.id][0]
            
            # Verify the song file exists before playing
            if not os.path.exists(song.filename):
                logger.error(f"Song file missing: {song.filename}")
                await interaction.channel.send(f"⚠️ Error: Could not play {song.title} (file missing)")
                # Try to play next song
                await self._play_next(guild, interaction)
                return

            self.queue_manager.current_songs[guild.id] = song
            await self.queue_manager.remove_song(guild.id, 0)

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

        except Exception as e:
            logger.error(f"Error in play_next: {e}")
            await interaction.channel.send("Failed to play next song.")

    async def _play_leave_sound(self, voice_client: discord.VoiceClient) -> None:
        try:
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

            # Start next song or prepare to leave
            if self.queue_manager.queues[interaction.guild_id]:
                logger.info(f"Playing next song in queue for guild: {guild_name}")
                await self._play_next(interaction.guild, interaction)
            elif interaction.guild.voice_client:
                logger.info(f"Queue empty, preparing to leave guild: {guild_name}")
                await self._play_leave_sound(interaction.guild.voice_client)

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
            await interaction.followup.send(embed=embed)
            return False

        # Send processing message
        processing_embed = discord.Embed(
            title="Processing Track",
            description="Downloading from Spotify...",
            color=discord.Color.blue()
        )
        processing_message = await interaction.followup.send(embed=processing_embed)
        
        # Directly download track from Spotify
        song = await self.spotify_client.download_track(url)
        if not song:
            error_embed = discord.Embed(
                title="Download Failed",
                description="Could not download track from Spotify.",
                color=discord.Color.red()
            )
            await processing_message.edit(embed=error_embed)
            return False

        await self.queue_manager.add_song(interaction.guild_id, song)
        
        voice_client = interaction.guild.voice_client
        if voice_client and not voice_client.is_playing():
            await self._play_next(interaction.guild, interaction)
        else:
            position = len(self.queue_manager.queues[interaction.guild_id])
            success_embed = discord.Embed(
                title="Track Added",
                description=f"Added to queue (Position: {position}): {song.title}",
                color=discord.Color.green()
            )
            if song.thumbnail:
                success_embed.set_thumbnail(url=song.thumbnail)
            success_embed.add_field(name="Duration", value=song.duration)
            
            # Update the processing message instead of sending a new one
            await processing_message.edit(embed=success_embed)
        
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
            await interaction.followup.send(embed=embed)
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
        
        # Get all tracks metadata first
        playlist_tracks = await self.spotify_client.get_playlist_tracks(playlist_id)
        if not playlist_tracks:
            embed = discord.Embed(
                title="No Tracks Found",
                description=f"No tracks found in playlist.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
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
            await interaction.followup.send(embed=embed)
            return False
        
        # Send initial progress embed
        status_embed = discord.Embed(
            title=f"Playlist: {playlist_name}",
            description=f"Processing tracks: 0/{len(playlist_tracks_page)}",
            color=discord.Color.blue()
        )
        
        # Add pagination info
        max_pages = (playlist_total + max_tracks - 1) // max_tracks
        if max_pages > 1:
            status_embed.add_field(
                name="Pagination", 
                value=f"Page {page}/{max_pages} • Total tracks: {playlist_total}"
            )
        
        # Add queued by information
        status_embed.set_footer(text=f"Queued by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
            
        status_message = await interaction.followup.send(embed=status_embed)
        
        # Process tracks one by one, starting playback as soon as the first track is available
        playlist_url = f"https://open.spotify.com/playlist/{playlist_id}"
        is_playing = False
        added_count = 0
        
        for i, track in enumerate(playlist_tracks_page):
            if not track:
                continue
                
            try:
                # Get the Spotify track URL
                track_id = track.get('id')
                if not track_id:
                    continue
                    
                track_url = f"https://open.spotify.com/track/{track_id}"
                
                # Update progress before downloading (so user knows which track is being processed)
                if (i + 1) % 5 == 0 or i == 0:
                    track_name = track.get('name', 'Unknown')
                    artist_name = track.get('artists', [{}])[0].get('name', 'Unknown Artist')
                    status_embed.description = f"Processing tracks: {added_count}/{len(playlist_tracks_page)}\nCurrently downloading: {artist_name} - {track_name}"
                    await status_message.edit(embed=status_embed)
                
                # Download the track
                song = await self.spotify_client.download_track(track_url)
                if not song:
                    continue
                    
                # Add song to queue
                await self.queue_manager.add_song(interaction.guild_id, song)
                added_count += 1
                
                # Start playing if this is the first track and nothing is currently playing
                voice_client = interaction.guild.voice_client
                if voice_client and not voice_client.is_playing() and not is_playing:
                    await self._play_next(interaction.guild, interaction)
                    is_playing = True
                    
                # Update progress message every 5 tracks or for important milestones
                if (i + 1) % 5 == 0 or (i == len(playlist_tracks_page) - 1 and added_count > 0):
                    status_embed.description = f"Added {added_count}/{len(playlist_tracks_page)} tracks to queue"
                    status_embed.color = discord.Color.green() if i == len(playlist_tracks_page) - 1 else discord.Color.blue()
                    await status_message.edit(embed=status_embed)

            except Exception as e:
                logger.error(f"Error processing playlist track: {e}")
                # Continue with next track

        # Final update with completion status
        if added_count > 0:
            status_embed.title = f"Playlist: {playlist_name} - Complete"
            status_embed.description = f"Successfully added {added_count}/{len(playlist_tracks_page)} tracks to queue"
            status_embed.color = discord.Color.green()
            
            # Add pagination info for next page if applicable
            if page < max_pages:
                status_embed.add_field(
                    name="More Tracks", 
                    value=f"Use `/play {url} page:{page+1}` for the next page",
                    inline=False
                )
                
            await status_message.edit(embed=status_embed)
        else:
            status_embed.title = f"Playlist Processing Failed"
            status_embed.description = f"Could not add any tracks from the playlist"
            status_embed.color = discord.Color.red()
            await status_message.edit(embed=status_embed)
                
        return added_count > 0

    async def _handle_spotify_album(self, url: str, interaction: discord.Interaction, page: int) -> bool:
        """Handle a Spotify album."""
        album_id = self.spotify_client.get_album_id(url)
        if not album_id:
            embed = discord.Embed(
                title="Invalid Album",
                description="Could not extract Spotify album ID.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
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
        
        # Get all album tracks first
        album_tracks = await self.spotify_client.get_album_tracks(album_id)
        if not album_tracks:
            embed = discord.Embed(
                title="No Tracks Found",
                description=f"No tracks found in album.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
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
            await interaction.followup.send(embed=embed)
            return False
        
        # Send initial progress embed
        status_embed = discord.Embed(
            title=f"Album: {album_display}",
            description=f"Processing tracks: 0/{len(album_tracks_page)}",
            color=discord.Color.blue()
        )
        if album_image:
            status_embed.set_thumbnail(url=album_image)
        
        # Add pagination info
        max_pages = (album_total + max_tracks - 1) // max_tracks
        if max_pages > 1:
            status_embed.add_field(
                name="Pagination", 
                value=f"Page {page}/{max_pages} • Total tracks: {album_total}"
            )
        
        # Add queued by information
        status_embed.set_footer(text=f"Queued by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
            
        status_message = await interaction.followup.send(embed=status_embed)
        
        # Process tracks one by one, starting playback as soon as the first track is available
        is_playing = False
        added_count = 0
        
        for i, track in enumerate(album_tracks_page):
            if not track:
                continue
                
            try:
                # Get the Spotify track URL
                track_id = track.get('id')
                if not track_id:
                    continue
                    
                track_url = f"https://open.spotify.com/track/{track_id}"
                
                # Update progress before downloading (so user knows which track is being processed)
                if (i + 1) % 5 == 0 or i == 0:
                    track_name = track.get('name', 'Unknown')
                    status_embed.description = f"Processing tracks: {added_count}/{len(album_tracks_page)}\nCurrently downloading: {track_name}"
                    await status_message.edit(embed=status_embed)
                
                # Download the track
                song = await self.spotify_client.download_track(track_url)
                if not song:
                    continue
                    
                # Set album metadata for the song
                if song.thumbnail is None and album_image:
                    song.thumbnail = album_image
                
                # Add song to queue
                await self.queue_manager.add_song(interaction.guild_id, song)
                added_count += 1
                
                # Start playing if this is the first track and nothing is currently playing
                voice_client = interaction.guild.voice_client
                if voice_client and not voice_client.is_playing() and not is_playing:
                    await self._play_next(interaction.guild, interaction)
                    is_playing = True
                    
                # Update progress message every 5 tracks or for important milestones
                if (i + 1) % 5 == 0 or (i == len(album_tracks_page) - 1 and added_count > 0):
                    status_embed.description = f"Added {added_count}/{len(album_tracks_page)} tracks to queue"
                    status_embed.color = discord.Color.green() if i == len(album_tracks_page) - 1 else discord.Color.blue()
                    await status_message.edit(embed=status_embed)
                    
            except Exception as e:
                logger.error(f"Error processing album track: {e}")
                # Continue with next track

        # Final update with completion status
        if added_count > 0:
            status_embed.title = f"Album: {album_display} - Complete"
            status_embed.description = f"Successfully added {added_count}/{len(album_tracks_page)} tracks to queue"
            status_embed.color = discord.Color.green()
            
            # Add pagination info for next page if applicable
            if page < max_pages:
                status_embed.add_field(
                    name="More Tracks", 
                    value=f"Use `/play {url} page:{page+1}` for the next page",
                    inline=False
                )
                
            await status_message.edit(embed=status_embed)
        else:
            status_embed.title = f"Album Processing Failed"
            status_embed.description = f"Could not add any tracks from the album"
            status_embed.color = discord.Color.red()
            await status_message.edit(embed=status_embed)
                
        return added_count > 0

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
            }
            
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

