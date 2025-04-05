import asyncio
import json
import logging
import os
from collections import defaultdict
from typing import Optional, Tuple
import itertools

import discord
import aiohttp
from discord.ext import commands, tasks
from discord import app_commands
from fake_useragent import UserAgent
import yt_dlp
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

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
COBALT_API_URL = os.getenv("COBALT_API_URL")
COBALT_API_KEY = os.getenv("COBALT_API_KEY")
# Custom Docker host alias, defaults to host.docker.internal
DOCKER_HOST_ALIAS = os.getenv("DOCKER_HOST_ALIAS", "host.docker.internal")
# Path to YouTube cookies file
YOUTUBE_COOKIES = os.path.join(os.path.dirname(__file__), "youtube_cookies.txt")
MAX_RETRY_ATTEMPTS = 3
RETRY_DELAY = 1
DEFAULT_VOLUME = float(os.getenv("DEFAULT_VOLUME", "1.0"))
MAX_VOLUME = float(os.getenv("MAX_VOLUME", "2.0"))
MAX_SONG_LENGTH = int(os.getenv("MAX_SONG_LENGTH", "900"))  # 15 minutes in seconds

class Song:
    def __init__(self, filename: str, title: str, duration: str, url: str, thumbnail: str):
        self.filename = filename
        self.title = title
        self.duration = duration
        self.url = url
        self.thumbnail = thumbnail

    @property
    def tuple(self) -> tuple:
        return (self.filename, self.title, self.duration, self.url, self.thumbnail)

class QueueManager:
    def __init__(self):
        self.queues = defaultdict(list)
        self.current_songs = {}
        self.file_use_count = defaultdict(int)
        self.volume = defaultdict(lambda: DEFAULT_VOLUME)
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

    def set_volume(self, guild_id: int, volume: float) -> None:
        self.volume[guild_id] = min(max(0.0, volume), MAX_VOLUME)

class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        
        super().__init__(command_prefix='/', intents=intents)
        self.queue_manager = QueueManager()
        self.tree.on_error = self.on_tree_error

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
        await self.change_presence(activity=discord.Game(name="your dog music fr"))
        self.presence_loop.start()

    async def setup_commands(self) -> None:
        """Register all music-related commands"""
        
        @self.tree.command(name="play", description="Play a song from URL")
        async def play(interaction: discord.Interaction, url: str):
            await interaction.response.defer()
            try:
                voice_client = await self._ensure_voice_client(interaction)
                if not voice_client:
                    await interaction.followup.send("Failed to join voice channel.")
                    return

                # Download the song first
                song = await self._download_song(url)
                if not song:
                    await interaction.followup.send("Failed to download the song.")
                    return

                await self.queue_manager.add_song(interaction.guild_id, song)
                
                if not voice_client.is_playing():
                    await self._play_next(interaction.guild, interaction)
                else:
                    # Send a queued message with position
                    position = len(self.queue_manager.queues[interaction.guild_id])
                    await interaction.followup.send(f"🎵 Added to queue (Position: {position}): {song.title}")

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

        @self.tree.command(name="volume", description="Set the volume (0-200%)")
        async def volume(interaction: discord.Interaction, percentage: int):
            if not interaction.guild.voice_client:
                await interaction.response.send_message("I'm not in a voice channel!")
                return

            if not 0 <= percentage <= 200:
                await interaction.response.send_message("Volume must be between 0 and 200!")
                return

            volume_float = percentage / 100.0
            self.queue_manager.set_volume(interaction.guild_id, volume_float)
            
            if interaction.guild.voice_client.source:
                interaction.guild.voice_client.source.volume = volume_float

            await interaction.response.send_message(f"🔊 Volume set to {percentage}%")

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
                    volume=self.queue_manager.volume[guild.id]
                )
                
                guild.voice_client.play(
                    audio_source,
                    after=lambda e: asyncio.run_coroutine_threadsafe(
                        self._after_play(e, interaction, song),
                        self.loop
                    )
                )
                
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
                    volume=1.1  # Set leave sound to 100% volume
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

        # Send as a new message instead of a follow-up
        await interaction.channel.send(embed=embed)

    async def _download_song(self, url: str) -> Optional[Song]:
        """Download a song from YouTube."""
        # First fetch metadata using yt-dlp (without downloading)
        metadata = await self._fetch_metadata(url)
        
        # Set default values if metadata fetch fails
        title = "Unknown Title"
        duration = "Unknown"
        thumbnail = None
        
        if metadata:
            title, duration, thumbnail = metadata
            logger.info(f"Using fetched metadata: {title}, {duration}")
        else:
            # If metadata fetch fails, we'll use default values and continue
            logger.warning(f"Metadata fetch failed for {url}, continuing with default values")
            # Try to extract some minimal info from the URL
            try:
                from urllib.parse import urlparse, parse_qs
                parsed_url = urlparse(url)
                if 'youtube.com' in parsed_url.netloc or 'youtu.be' in parsed_url.netloc:
                    if 'youtu.be' in parsed_url.netloc:
                        video_id = parsed_url.path[1:]  # remove leading slash
                    else:
                        query = parse_qs(parsed_url.query)
                        video_id = query.get('v', ['Unknown'])[0]
                    title = f"YouTube-{video_id}"
                else:
                    title = f"Song from {parsed_url.netloc}"
            except Exception as e:
                logger.error(f"Error parsing URL for minimal info: {e}")
                title = "Unknown Song"
        
        # Try to download using yt-dlp first
        logger.info(f"Attempting to download audio via yt-dlp: {url}")
        ytdlp_result = await self._download_via_ytdlp(url)
        if ytdlp_result:
            return ytdlp_result
        
        # Fallback to Cobalt if yt-dlp fails
        logger.info(f"yt-dlp failed, falling back to Cobalt for: {url}")
        return await self._download_via_cobalt(url, title, duration, thumbnail)

    async def _fetch_metadata(self, url: str) -> Optional[Tuple[str, str, str]]:
        """Fetch only metadata from a URL using yt-dlp without downloading the file."""
        ydl_opts = {
            'format': 'bestaudio',
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'skip_download': True,  # Skip downloading the actual file
            'noplaylist': True,
            'cookiefile': YOUTUBE_COOKIES,  # Use YouTube cookies for authentication
        }

        try:
            # Check if cookie file exists
            if os.path.exists(YOUTUBE_COOKIES):
                logger.info(f"Using YouTube cookies file: {YOUTUBE_COOKIES}")
            else:
                logger.warning(f"YouTube cookies file not found at: {YOUTUBE_COOKIES}")
                
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                logger.info(f"Fetching metadata for: {url}")
                info = await asyncio.get_event_loop().run_in_executor(
                    ThreadPoolExecutor(1),
                    lambda: ydl.extract_info(url, download=False)
                )
                
                if not info:
                    logger.error("No info returned from yt-dlp")
                    return None

                title = info.get('title', 'Unknown Title')
                
                # Format duration as mm:ss
                duration_sec = info.get('duration', 0)
                if duration_sec:
                    minutes = int(duration_sec // 60)
                    seconds = int(duration_sec % 60)
                    duration = f"{minutes}:{seconds:02d}"
                else:
                    duration = "Unknown"
                    
                thumbnail = info.get('thumbnail')
                
                logger.info(f"Metadata fetched: {title}, {duration}")
                return title, duration, thumbnail

        except Exception as e:
            logger.error(f"Error fetching metadata: {str(e)}")
            return None
        
    async def _download_via_cobalt(self, url: str, title: str, duration: str, thumbnail: str) -> Optional[Song]:
        """Attempt to download audio using the Cobalt API."""
        if not COBALT_API_URL:
            logger.warning("COBALT_API_URL not configured, skipping Cobalt.")
            return None

        cobalt_endpoint = COBALT_API_URL.rstrip('/')
        
        # Translation for Docker: If Cobalt API URL uses localhost/127.0.0.1, replace with host.docker.internal
        if "://127.0.0.1:" in cobalt_endpoint or "://localhost:" in cobalt_endpoint:
            original_endpoint = cobalt_endpoint
            # Extract the original host and port
            parts = cobalt_endpoint.split("://")
            if len(parts) == 2:
                protocol = parts[0]
                rest = parts[1]
                # Find where the host:port ends
                if "/" in rest:
                    host_port, path = rest.split("/", 1)
                    # Replace localhost or 127.0.0.1 with host.docker.internal, keeping the original port
                    if ":" in host_port:
                        host, port = host_port.split(":", 1)
                        if host in ["127.0.0.1", "localhost"]:
                            new_host_port = f"{DOCKER_HOST_ALIAS}:{port}"
                            cobalt_endpoint = f"{protocol}://{new_host_port}/{path}"
                            logger.info(f"Translated Cobalt API URL from {original_endpoint} to {cobalt_endpoint} for Docker")
                else:
                    # No path, just host:port
                    host_port = rest
                    if ":" in host_port:
                        host, port = host_port.split(":", 1)
                        if host in ["127.0.0.1", "localhost"]:
                            new_host_port = f"{DOCKER_HOST_ALIAS}:{port}"
                            cobalt_endpoint = f"{protocol}://{new_host_port}"
                            logger.info(f"Translated Cobalt API URL from {original_endpoint} to {cobalt_endpoint} for Docker")
        
        payload = {
            "url": url,
            "downloadMode": "audio",
            "audioFormat": "mp3",
            "audioBitrate": "320",
            "videoQuality": "1080",
            "filenameStyle": "basic"
        }
        
        logger.info(f"Attempting download via Cobalt: {url}")

        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    'Accept': 'application/json',
                    'Content-Type': 'application/json'
                }
                
                # Add API key authentication if configured
                if COBALT_API_KEY and COBALT_API_KEY.strip():
                    headers['Authorization'] = f'Api-Key {COBALT_API_KEY}'
                    
                async with session.post(cobalt_endpoint, json=payload, headers=headers, timeout=30) as response:
                    if response.status != 200:
                        try:
                            error_text = await response.text()
                            error_data = json.loads(error_text)
                            if error_data.get("status") == "error" and "error" in error_data:
                                error_info = error_data["error"]
                                error_code = error_info.get("code", "Unknown")
                                error_context = error_info.get("context", {})
                                logger.error(f"Cobalt API request failed with status {response.status}: {error_code} - Context: {error_context}")
                            else:
                                logger.error(f"Cobalt API request failed with status {response.status}: {error_text}")
                        except (json.JSONDecodeError, KeyError):
                            logger.error(f"Cobalt API request failed with status {response.status}: {await response.text()}")
                        return None

                    data = await response.json()
                    status = data.get("status")
                    
                    logger.debug(f"Cobalt response: {data}")
                    logger.debug(f"Cobalt response status: {status}")

                    media_url = None

                    if status == "stream":
                        media_url = data.get("url")
                    elif status == "redirect":
                        media_url = data.get("url")
                        if not media_url:
                            logger.warning("Cobalt redirect status without URL.")
                            return None
                    elif status == "tunnel":
                        media_url = data.get("url")
                        if not media_url:
                            logger.warning("Cobalt tunnel status without URL.")
                            return None
                        logger.info(f"Cobalt provided tunnel URL: {media_url}")
                    elif status == "picker":
                        picker_items = data.get("picker", [])
                        if picker_items and picker_items[0].get("url"):
                            media_url = picker_items[0]["url"]
                        else:
                            logger.warning("Cobalt picker status with no suitable items.")
                            return None
                    elif status == "error":
                        error_info = data.get("error", {})
                        error_code = error_info.get("code", "Unknown error")
                        error_context = error_info.get("context", {})
                        logger.warning(f"Cobalt API returned error: {error_code} - Context: {error_context}")
                        return None
                    else:
                        logger.warning(f"Unhandled Cobalt status: {status} - Full response: {data}")
                        return None

                    if not media_url:
                        logger.warning("Cobalt did not provide a usable media URL.")
                        return None

                    # Check if we got a placeholder domain in the URL
                    if "api.url.example" in media_url or (not media_url.startswith("http") and "/tunnel?" in media_url):
                        if not media_url.startswith("http"):
                            logger.warning(f"Detected relative tunnel URL: {media_url}")
                        else:
                            logger.warning(f"Detected placeholder domain in Cobalt URL: {media_url}")
                        
                        # Extract the actual API domain from COBALT_API_URL
                        cobalt_domain = None
                        if COBALT_API_URL:
                            try:
                                from urllib.parse import urlparse
                                parsed_cobalt_api = urlparse(COBALT_API_URL)
                                cobalt_domain = f"{parsed_cobalt_api.scheme}://{parsed_cobalt_api.netloc}"
                                logger.info(f"Extracted actual domain from COBALT_API_URL: {cobalt_domain}")
                            except Exception as e:
                                logger.error(f"Failed to parse COBALT_API_URL: {e}")
                        
                        if cobalt_domain:
                            # Handle relative URLs
                            if not media_url.startswith("http"):
                                if media_url.startswith("/"):
                                    corrected_url = f"{cobalt_domain}{media_url}"
                                else:
                                    corrected_url = f"{cobalt_domain}/{media_url}"
                                logger.info(f"Corrected relative URL to absolute URL: {corrected_url}")
                                media_url = corrected_url
                            else:
                                # Replace placeholder domain with actual domain
                                from urllib.parse import urlparse, urlunparse
                                parsed_media_url = urlparse(media_url)
                                parsed_cobalt = urlparse(cobalt_domain)
                                
                                # Build new URL with correct domain but keep path and query
                                corrected_url = urlunparse((
                                    parsed_cobalt.scheme,
                                    parsed_cobalt.netloc,
                                    parsed_media_url.path,
                                    parsed_media_url.params,
                                    parsed_media_url.query,
                                    parsed_media_url.fragment
                                ))
                                
                                logger.info(f"Corrected domain in URL from {media_url} to {corrected_url}")
                                media_url = corrected_url
                        else:
                            logger.error("Could not correct URL, no valid COBALT_API_URL available")
                            return None

                    # Translation for Docker: If media_url uses localhost/127.0.0.1, replace with host.docker.internal
                    if "://127.0.0.1:" in media_url or "://localhost:" in media_url:
                        original_url = media_url
                        # Extract the original host and port
                        parts = media_url.split("://")
                        if len(parts) == 2:
                            protocol = parts[0]
                            rest = parts[1]
                            # Find where the host:port ends
                            if "/" in rest:
                                host_port, path = rest.split("/", 1)
                                # Replace localhost or 127.0.0.1 with host.docker.internal, keeping the original port
                                if ":" in host_port:
                                    host, port = host_port.split(":", 1)
                                    if host in ["127.0.0.1", "localhost"]:
                                        new_host_port = f"{DOCKER_HOST_ALIAS}:{port}"
                                        media_url = f"{protocol}://{new_host_port}/{path}"
                                        logger.info(f"Translated Cobalt URL from {original_url} to {media_url} for Docker")
                    
                    logger.info(f"Cobalt provided media URL: {media_url}")

                # Download content from the URL Cobalt provided
                try:
                    # Generate a unique filename for the downloaded song
                    filename = f"downloads/{title.replace('/', '_').replace(':', '_')}.mp3"
                    
                    if not os.path.exists(os.path.dirname(filename)):
                        os.makedirs(os.path.dirname(filename))
                        
                    async with session.get(media_url, timeout=60) as media_response:
                        if media_response.status == 200:
                            with open(filename, 'wb') as f:
                                audio_content = await media_response.read()
                                f.write(audio_content)
                            logger.info(f"Successfully downloaded audio via Cobalt: {len(audio_content)} bytes to {filename}")
                            
                            # Use the metadata we obtained earlier
                            return Song(
                                filename=filename,
                                title=title,
                                duration=duration,
                                url=url,
                                thumbnail=thumbnail
                            )
                        else:
                            logger.error(f"Failed to download media from Cobalt URL ({media_url}): HTTP {media_response.status}")
                            return None
                except Exception as e:
                    logger.error(f"Error downloading or saving Cobalt audio: {e}", exc_info=True)
                    return None

        except aiohttp.ClientError as e:
            logger.error(f"Network error during Cobalt request or download: {e}")
            return None
        except asyncio.TimeoutError:
            logger.error("Timeout during Cobalt request or download.")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"Failed to decode Cobalt API JSON response: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error during Cobalt processing: {e}", exc_info=True)
            return None
            
    async def _download_via_ytdlp(self, url: str) -> Optional[Song]:
        """Download audio from a URL using yt-dlp as a fallback."""
        ydl_opts = {
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'outtmpl': 'downloads/%(title)s.%(ext)s',
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'noplaylist': True,
            # Only download audio stream
            'format_sort': ['acodec:mp3:128'],
            'postprocessor_args': ['-ar', '44100'],
            'prefer_ffmpeg': True,
            'cookiefile': YOUTUBE_COOKIES,  # Use YouTube cookies for authentication
        }

        try:
            # Check if cookie file exists before using it
            if os.path.exists(YOUTUBE_COOKIES):
                logger.info(f"Using YouTube cookies file for download: {YOUTUBE_COOKIES}")
            else:
                logger.warning(f"YouTube cookies file not found at: {YOUTUBE_COOKIES}")
                
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
        
        # Log Cobalt availability
        if COBALT_API_URL:
            logger.info(f"Cobalt API configured at: {COBALT_API_URL}")
            if COBALT_API_KEY and COBALT_API_KEY.strip():
                logger.info("Cobalt API authentication configured")
            else:
                logger.info("Cobalt API authentication not configured")
        else:
            logger.warning("COBALT_API_URL not set. Will use yt-dlp only.")
            
        logger.info("Starting bot...")
        bot = MusicBot()
        bot.run(BOT_TOKEN)
    except Exception as e:
        logger.exception(f"Fatal error during bot startup: {e}")
        exit(1)
