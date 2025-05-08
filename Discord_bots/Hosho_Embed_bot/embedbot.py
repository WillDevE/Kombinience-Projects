import asyncio
import os
import re
import json
from typing import Optional, Tuple
from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler
import urllib.parse
import itertools
import tempfile
import time
import io

from dotenv import load_dotenv

import aiohttp
import discord
import yt_dlp

from discord.ext import commands, tasks

# Custom exception for file size errors
class FileSizeExceededError(Exception):
    """Custom exception for when a file exceeds the Discord upload limit."""
    def __init__(self, file_size: int, limit: int, message: str = "File exceeds Discord upload limit"):
        self.file_size = file_size
        self.limit = limit
        self.message = message
        super().__init__(self.message)

    def __str__(self):
        return f"{self.message}: {self.file_size} bytes > {self.limit} bytes (Limit: {self.limit / (1024*1024):.1f}MB)"

# Set up logging
def setup_logging():
    """Configure logging with both file and console handlers."""
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    log_file = os.path.join(log_dir, "embedbot.log")
    
    # Create formatters and handlers
    file_formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s - {%(filename)s:%(lineno)d}'
    )
    console_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s'
    )
    
    # File handler with rotation
    file_handler = RotatingFileHandler(
        log_file, maxBytes=10*1024*1024, backupCount=5
    )
    file_handler.setFormatter(file_formatter)
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(console_formatter)
    
    # Set up the logger
    logger = logging.getLogger('embedbot')
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger

# Initialize logger
logger = setup_logging()

# --- Configuration ---
load_dotenv()
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ALLOWED_GUILDS = [int(id) for id in os.getenv("ALLOWED_GUILDS", "").split(",") if id]
COBALT_API_URL = os.getenv("COBALT_API_URL")
# Custom Docker host alias, defaults to host.docker.internal
DOCKER_HOST_ALIAS = os.getenv("DOCKER_HOST_ALIAS", "host.docker.internal")
# New configuration for Discord file size limit
DISCORD_FILE_LIMIT_MB = float(os.getenv("DISCORD_FILE_LIMIT_MB", "10"))
DISCORD_FILE_LIMIT_BYTES = int(DISCORD_FILE_LIMIT_MB * 1024 * 1024)
# Path for the JSON counter file
COUNTER_JSON_FILE = os.path.join(os.path.dirname(__file__), "video_counter.json")

def load_config() -> dict:
    """Load configuration from config.json."""
    try:
        config_path = os.path.join(os.path.dirname(__file__), 'config.json')
        if not os.path.exists(config_path):
            logger.warning("config.json not found")
            return {}
            
        with open(config_path, 'r') as f:
            config = json.load(f)
        return config
    except Exception as e:
        logger.error(f"Error loading config: {e}")
        return {}

# --- Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=commands.when_mentioned_or("!"), intents=intents)

# Status messages for the bot to cycle through
STATUS_MESSAGES = itertools.cycle([
    "im stuck on a pi 4 2gb",
    "I need to scroll",
    "say that again",
    "funny bot status",
    "act like an angel & dress like crazy",
    "King von anti piracy screen",
    "Now with Cobalt!"
])

@tasks.loop(seconds=30)
async def presence_loop():
    """Update the bot's status message every 30 seconds."""
    try:
        status = next(STATUS_MESSAGES)
        await bot.change_presence(activity=discord.CustomActivity(name=status))
    except Exception as e:
        logger.error(f"Error in presence loop: {e}")

# --- URL Matching ---
TIKTOK_REGEX = re.compile(r"https?://(?:www\.|vt\.)?tiktok\.com/.*")
FACEBOOK_REGEX = re.compile(r"https?://(?:www\.|m\.|business\.)?facebook\.com/.*")
INSTAGRAM_REGEX = re.compile(r"https?://(?:www\.)?instagram\.com/(?:p|reel)/([a-zA-Z0-9_-]+)(?:/.*)?")

# --- Download Functions ---

async def download_via_cobalt(url: str) -> Optional[Tuple[bytes, str]]:
    """Attempt to download video content using the Cobalt API."""
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
        "downloadMode": "auto",
        "videoQuality": "1080",
        "youtubeVideoCodec": "h264"
    }
    
    logger.info(f"Attempting download via Cobalt: {url}")

    try:
        async with aiohttp.ClientSession() as session:
            headers = {
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            }
            async with session.post(cobalt_endpoint, json=payload, headers=headers, timeout=30) as response:
                if response.status != 200:
                    logger.error(f"Cobalt API request failed with status {response.status}: {await response.text()}")
                    return None

                data = await response.json()
                status = data.get("status")
                
                logger.debug(f"Cobalt response: {data}")
                logger.debug(f"Cobalt response status: {status}")

                media_url = None
                title = "Video"  # Default title

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
                     # Use filename if provided
                     if data.get("filename"):
                         title = data.get("filename")
                     logger.info(f"Cobalt provided tunnel URL: {media_url}")
                elif status == "picker":
                     picker_items = data.get("picker", [])
                     if picker_items and picker_items[0].get("url"):
                         media_url = picker_items[0]["url"]
                         title = picker_items[0].get("title", "Video")
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
            async with session.get(media_url, timeout=60) as media_response:
                if media_response.status == 200:
                    video_content = await media_response.read()
                    logger.info(f"Successfully downloaded video via Cobalt: {len(video_content)} bytes")
                    return video_content, title
                else:
                    logger.error(f"Failed to download media from Cobalt URL ({media_url}): HTTP {media_response.status}")
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


async def download_via_ytdlp(url: str) -> Optional[Tuple[bytes, str]]:
    """Download video content using yt-dlp as a fallback."""
    logger.info(f"Falling back to yt-dlp for URL: {url}")
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            # Increased ytdlp internal limit slightly above common Discord limits to catch more,
            # but the main check is done after download.
            # User requested 10MB, Discord non-nitro is 25MB. Setting ytdlp to 50M as a soft cap.
            # The hard cap DISCORD_FILE_LIMIT_BYTES will be checked after download.
            # The original 500M was a very loose cap.
            ydl_opts = {
                'format': 'bestvideo[ext=mp4][filesize<=50M]+bestaudio[ext=m4a]/best[ext=mp4][filesize<=50M]/best[filesize<=50M]',
                'outtmpl': os.path.join(temp_dir, 'video.%(ext)s'),
                'quiet': True,
                'noplaylist': True,
                'writesubtitles': False,
                'writeautomaticsub': False,
                'no_warnings': True,
                'logtostderr': False,
                'verbose': False,
                'no_progress': True,
                 'http_headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                 }
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    info = ydl.extract_info(url, download=True)
                    downloaded_files = [f for f in os.listdir(temp_dir) if f.startswith('video.')]
                    if not downloaded_files:
                        raise Exception("yt-dlp downloaded, but no video file found.")

                    video_path = os.path.join(temp_dir, downloaded_files[0])

                    with open(video_path, 'rb') as f:
                        video_content = f.read()
                    logger.info(f"Successfully downloaded video via yt-dlp: {len(video_content)} bytes")

                    # Check size after download - this is a pre-check, main check in process_video_url
                    if len(video_content) > 100 * 1024 * 1024:  # Generous pre-limit e.g. 100 MB
                         logger.warning(f"yt-dlp downloaded file initially exceeds 100MB ({len(video_content)/(1024*1024):.2f} MB). Further check needed.")
                         # We don't return None here, let process_video_url handle the precise DISCORD_FILE_LIMIT_BYTES check

                    return video_content, info.get('title', 'Video')
                except yt_dlp.utils.DownloadError as e:
                    logger.error(f"yt-dlp download error: {e}")
                    return None
                except Exception as e:
                    logger.error(f"Generic error during yt-dlp download: {e}", exc_info=True)
                    return None
    except Exception as e:
        logger.error(f"Error setting up temp dir or yt-dlp: {e}", exc_info=True)
        return None


async def get_video_content(url: str) -> Optional[Tuple[bytes, str]]:
    """
    Downloads video content, trying Cobalt first and falling back to yt-dlp.
    Returns (video_content, video_title) or None if download fails.
    """
    # Try Cobalt first
    cobalt_result = await download_via_cobalt(url)
    if cobalt_result:
        logger.info("Successfully obtained video content via Cobalt.")
        return cobalt_result

    # Fallback to yt-dlp
    logger.info("Cobalt failed or skipped, trying yt-dlp...")
    ytdlp_result = await download_via_ytdlp(url)
    if ytdlp_result:
         logger.info("Successfully obtained video content via yt-dlp.")
         return ytdlp_result

    # If both failed
    logger.error(f"Failed to download video content from {url} using both Cobalt and yt-dlp.")
    return None


# Modified process_video_url to check size and return content for direct upload
async def process_video_url(original_url: str, author_name: str = "") -> tuple[bytes, str]:
    """
    Process a video URL using Cobalt/yt-dlp.
    Checks if the video is within Discord's file size limit.
    Returns (video_content, video_title) or raises FileSizeExceededError or other exceptions.
    """
    logger.info(f"Processing video URL from {author_name}: {original_url}")

    download_result = await get_video_content(original_url)

    if not download_result:
        logger.error(f"Failed to get video content for {original_url}")
        raise Exception("Failed to download video content")

    video_content, video_title = download_result

    # Check size against Discord's limit
    if len(video_content) > DISCORD_FILE_LIMIT_BYTES:
        logger.warning(f"Video content for {original_url} exceeds Discord limit: {len(video_content)} bytes > {DISCORD_FILE_LIMIT_BYTES} bytes.")
        raise FileSizeExceededError(file_size=len(video_content), limit=DISCORD_FILE_LIMIT_BYTES)

    logger.info(f"Video content for {original_url} is within Discord limit ({len(video_content)} bytes). Title: {video_title}")
    return video_content, video_title


# --- Helper Functions ---
def get_video_provider(url: str) -> str:
    """Determines the video provider based on the original URL."""
    if TIKTOK_REGEX.search(url):
        return "TikTok"
    elif FACEBOOK_REGEX.search(url):
        return "Facebook"
    elif INSTAGRAM_REGEX.search(url):
        return "Instagram"
    else:
        # Try to get domain name as fallback provider
        try:
             domain = urllib.parse.urlparse(url).netloc
             if domain.startswith('www.'):
                 domain = domain[4:]
             return domain.split('.')[0].capitalize() if '.' in domain else "Link"
        except:
             return "Link"  # Generic fallback

# New helper function to sanitize filenames
def sanitize_filename(filename: str, default_name: str = "video.mp4") -> str:
    """Sanitizes a string to be a valid filename."""
    if not filename:
        filename = default_name
    
    # Remove characters that are typically problematic in filenames
    # This is a basic sanitization. For more robust, consider a library or more extensive regex.
    sanitized = re.sub(r'[\\/*?:"<>|]',"", filename)
    # Replace sequences of whitespace with a single underscore
    sanitized = re.sub(r'\s+', '_', sanitized)
    # Limit length (Discord has its own limits, but this is a general precaution)
    sanitized = sanitized[:200] # Max filename length sanity
    
    # Ensure it's not empty after sanitization
    if not sanitized.strip("._"):
        sanitized = default_name
        
    # Ensure it has a common video extension if not already present
    if not re.search(r'\.(mp4|mov|webm|mkv|avi)$', sanitized, re.IGNORECASE):
        sanitized += ".mp4"
        
    return sanitized

# New function to get and increment video number from JSON file
async def get_next_video_number() -> int:
    """Get the next video number, reading from and writing to a JSON file."""
    current_number = 0
    try:
        if os.path.exists(COUNTER_JSON_FILE):
            with open(COUNTER_JSON_FILE, 'r') as f:
                try:
                    data = json.load(f)
                    current_number = data.get("video_count", 0)
                except json.JSONDecodeError:
                    logger.warning(f"{COUNTER_JSON_FILE} is not valid JSON. Initializing count.")
                    current_number = 0 # Initialize if JSON is malformed
        else:
            logger.info(f"{COUNTER_JSON_FILE} not found. Initializing count.")
            current_number = 0 # Initialize if file doesn't exist

        next_number = current_number + 1

        with open(COUNTER_JSON_FILE, 'w') as f:
            json.dump({"video_count": next_number}, f, indent=4)
        
        logger.debug(f"Video counter updated to: {next_number}")
        return next_number

    except Exception as e:
        logger.error(f"Error managing video counter file {COUNTER_JSON_FILE}: {e}", exc_info=True)
        # Fallback in case of file I/O errors to prevent breaking the main flow
        # Using a timestamp-based fallback, though less ideal for exact matching with old system
        return int(time.time() * 1000) % 1000000 # Returns a number to avoid breaking message format

# --- Bot Events ---
@bot.event
async def on_ready():
    logger.info(f"Bot successfully logged in as {bot.user} (ID: {bot.user.id})")
    if not presence_loop.is_running():
        presence_loop.start()
    logger.info("------")


# --- Message Processing ---
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Only check guild ID if not in DMs and ALLOWED_GUILDS is configured
    if message.guild and ALLOWED_GUILDS and message.guild.id not in ALLOWED_GUILDS:
        return

    # Extract URLs using existing regexes
    urls_found = set()
    
    # Combine regexes for efficiency
    combined_regex = re.compile(
        f"({TIKTOK_REGEX.pattern})|({FACEBOOK_REGEX.pattern})|({INSTAGRAM_REGEX.pattern})"
    )

    # Find all potential matches
    for match in combined_regex.finditer(message.content):
        url = match.group(0)
        if url:
             urls_found.add(url)

    if urls_found:
        logger.info(f"Found {len(urls_found)} potential video URL(s) in message from {message.author} ({message.author.id}) in channel {message.channel.id}")
        # Process each unique URL found
        for url in urls_found:
             # Get the message content excluding *all* found URLs for clarity
             clean_content = message.content
             for found_url in urls_found:
                  clean_content = clean_content.replace(found_url, '')
             clean_content = clean_content.strip()

             # Start handling this specific URL
             asyncio.create_task(handle_video_url(message, url, clean_content))


async def handle_video_url(message: discord.Message, original_url: str, message_content: str = ""):
    """Handles the processing of a video URL message, direct Discord upload, and feedback."""
    logger.info(f"Handling URL: {original_url} from message {message.id} by {message.author.display_name}")

    processing_reaction_added = False
    try:
        await message.add_reaction("⏳")
        processing_reaction_added = True
    except discord.Forbidden:
         logger.warning(f"Missing permissions to add reactions in channel {message.channel.id}")
    except discord.NotFound:
         logger.warning(f"Original message {message.id} not found, likely deleted.")
         return
    except Exception as e:
        logger.error(f"Failed to add processing reaction to message {message.id}: {e}", exc_info=True)

    # success_overall means the URL was handled (either uploaded, or confirmed too large, or error acknowledged)
    success_overall = False 
    # uploaded_to_discord specifically tracks if the file was sent to Discord
    uploaded_to_discord = False
    max_retries = 2 # Retries for download/processing errors, not for "file too large"
    last_error = None

    for attempt in range(max_retries):
        try:
            # Process the video URL (downloads and checks size)
            video_content, video_title = await process_video_url(original_url, message.author.display_name)

            sanitized_title = sanitize_filename(video_title if video_title else "video")
            discord_file = discord.File(io.BytesIO(video_content), filename=sanitized_title)
            
            author_name = message.author.display_name
            provider_name = get_video_provider(original_url)
            video_number_str = ""
            try:
                video_num = await get_next_video_number()
                video_number_str = f" *#{video_num}*" # Add space before for separation
            except Exception as e_num:
                logger.error(f"Failed to get video number: {e_num}")
                # video_number_str remains empty if there's an error

            # Wrap original_url in <> to suppress its embed
            text_link_part = f"([Original {provider_name}](<{original_url}>))"
            
            # Construct reply text, ensuring it fits within Discord's 2000 char limit
            # Example: "User: Some text here ([Original TikTok](url)) *#123*"
            # available_length_for_user_text = 2000 - (len(author_name) + ": " + space + link_part + space + video_number_str + safety_margin)
            base_elements_len = len(author_name) + 2 + 1 + len(text_link_part) + len(video_number_str) + 10 # +2 for ": ", +1 for space before link, +10 safety
            available_length_for_user_text = 2000 - base_elements_len

            if message_content: # User's text accompanying the URL
                if len(message_content) > available_length_for_user_text:
                    truncated_message_content = message_content[:available_length_for_user_text-3] + "..."
                else:
                    truncated_message_content = message_content
                reply_text = f"{author_name}: {truncated_message_content} {text_link_part}{video_number_str}"
            else:
                reply_text = f"{author_name}: {text_link_part}{video_number_str}"

            # Send the message with the video file
            await message.channel.send(content=reply_text, file=discord_file, allowed_mentions=discord.AllowedMentions.none())
            logger.info(f"Successfully uploaded video for {original_url} from {author_name} directly to Discord channel {message.channel.id}")

            # Delete original message (only in guilds, check permissions)
            if message.guild:
                try:
                    bot_member = message.guild.me
                    if bot_member.guild_permissions.manage_messages:
                         await message.delete()
                         logger.debug(f"Deleted original message {message.id}")
                    else:
                         logger.warning(f"Missing 'Manage Messages' permission in guild {message.guild.id} to delete original message {message.id}.")
                except discord.Forbidden:
                     logger.warning(f"Missing permissions to delete message {message.id} in channel {message.channel.id}")
                except discord.NotFound:
                     logger.warning(f"Original message {message.id} not found when attempting delete.")
                except Exception as e:
                     logger.error(f"Failed to delete message {message.id}: {e}", exc_info=True)

            success_overall = True
            uploaded_to_discord = True
            break # Exit retry loop on success

        except FileSizeExceededError as e:
            last_error = e
            logger.warning(f"Video from {original_url} is too large for Discord upload: {e.file_size} bytes. Limit is {DISCORD_FILE_LIMIT_BYTES} bytes ({DISCORD_FILE_LIMIT_MB}MB).")
            
            # This case is a "successful" handling, just not an upload. No retries needed.
            # Reply to user about the size limit
            limit_mb_str = f"{DISCORD_FILE_LIMIT_MB:.1f}".rstrip('0').rstrip('.')
            # Wrap original_url in <> to suppress its embed in the error message
            reply_msg = f"Sorry {message.author.mention}, the video from <{original_url}> is too large for me to upload to Discord (my limit is {limit_mb_str}MB)."
            try:
                await message.reply(reply_msg, mention_author=False, allowed_mentions=discord.AllowedMentions(users=[message.author]))
            except Exception as reply_err:
                logger.error(f"Failed to send 'file too large' reply for {original_url}: {reply_err}")

            success_overall = True # The "too large" situation is handled.
            break # Exit retry loop

        except Exception as e: # Catch other errors (download, cobalt, etc.)
            last_error = e
            logger.error(f"Attempt {attempt + 1}/{max_retries} failed for {original_url}: {e}", exc_info=True)
            if attempt < max_retries - 1:
                wait_time = 1.5 ** attempt # Exponential backoff
                logger.info(f"Retrying in {wait_time:.2f} seconds...")
                await asyncio.sleep(wait_time)
            # If it's the last attempt and still fails, success_overall remains False

    # Cleanup reactions
    if processing_reaction_added:
        try:
            await message.remove_reaction("⏳", bot.user)
        except Exception: # nosemgrep: general-exception-without-logging # Logging not critical here
             pass # Ignore if already removed or message deleted

    if not success_overall: # All retries for general errors failed
        logger.error(f"All {max_retries} attempts failed for {original_url}. Last error: {last_error}")
        try:
            await message.add_reaction("❌")
        except Exception: # nosemgrep: general-exception-without-logging
             pass
    elif uploaded_to_discord: # Successful upload
        try:
            # Optionally add a success reaction if desired, or just rely on the message being sent.
            # await message.add_reaction("✅") 
            pass
        except Exception: # nosemgrep: general-exception-without-logging
            pass
    elif success_overall and not uploaded_to_discord: # This means it was too large (FileSizeExceededError)
        try:
            await message.add_reaction("❌") # Already added in the except block, but as a fallback.
        except Exception: # nosemgrep: general-exception-without-logging
            pass


# Start the bot
if __name__ == "__main__":
     if not BOT_TOKEN:
          logger.critical("DISCORD_BOT_TOKEN environment variable not set.")
     elif not COBALT_API_URL:
          logger.warning("COBALT_API_URL environment variable not set. Cobalt functionality will be disabled.")
          logger.info(f"Starting Discord bot (Cobalt disabled, Discord Upload Limit: {DISCORD_FILE_LIMIT_MB}MB)...")
          bot.run(BOT_TOKEN)
     else:
          logger.info(f"Starting Discord bot (Discord Upload Limit: {DISCORD_FILE_LIMIT_MB}MB)...")
          bot.run(BOT_TOKEN)
