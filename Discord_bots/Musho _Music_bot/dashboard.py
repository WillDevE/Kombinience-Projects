import os
import json
from datetime import datetime
from flask import Flask, render_template, jsonify, request, redirect, url_for
import threading
import logging

# Set up Flask app
app = Flask(__name__, 
    static_folder="static", 
    template_folder="templates"
)

# Set URL prefix for all routes
URL_PREFIX = '/musho'

# Configure WSGI application for more reliable performance
app.config['PROPAGATE_EXCEPTIONS'] = True
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max upload

# Add a direct root route for debugging
@app.route('/')
def root_debug():
    """Root-level route for debugging Docker network connectivity"""
    return "Dashboard root debugging route is working", 200

# Register error handler for exceptions
@app.errorhandler(Exception)
def handle_exception(e):
    """Handle all unhandled exceptions"""
    logger.error(f"Unhandled exception in Flask app: {str(e)}", exc_info=True)
    return jsonify({"error": "Internal server error", "message": str(e)}), 500

# Add a direct healthcheck route for debugging without URL prefix
@app.route('/healthcheck')
def direct_healthcheck():
    """Direct healthcheck without URL prefix for debugging Docker networking"""
    return "Dashboard server direct healthcheck: OK", 200

# Make sure these functions are properly exposed for import
__all__ = ['register_bot', 'record_song_played', 'start_dashboard']

# Configure logging with a simpler format for troubleshooting
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Dashboard data storage
dashboard_data = {
    'bot_stats': {
        'uptime': 0,
        'start_time': datetime.now(),
        'start_timestamp': int(datetime.now().timestamp()),  # Add Unix timestamp for client-side calculations
        'guilds': 0,
        'total_songs_played': 0,
        'active_voice_channels': 0,
        'last_updated': int(datetime.now().timestamp())  # Add last updated timestamp for client-side change detection
    },
    'song_history': [],
    'guild_stats': {},
    'top_songs': [],
    'server_stats': {} # New structure for per-server statistics
}

# Track which data has changed to avoid unnecessary updates
data_changed = {
    'bot_stats': True,
    'song_history': False,
    'guild_stats': False
}

# Bot state management
bot_instance = None

def register_bot(bot):
    """Register the bot instance with the dashboard"""
    global bot_instance, dashboard_data, data_changed
    bot_instance = bot
    
    # Set the start timestamp once
    dashboard_data['bot_stats']['start_timestamp'] = int(datetime.now().timestamp())
    dashboard_data['bot_stats']['start_time'] = datetime.now()
    
    # Force initial update
    for key in data_changed:
        data_changed[key] = True
    
    update_stats()
    logger.info("Bot registered with dashboard")

def update_stats():
    """Update dashboard statistics from bot data"""
    global bot_instance, dashboard_data, data_changed

    if not bot_instance:
        return

    try:
        # Capture current time once for all operations
        current_time = datetime.now()
        current_timestamp = int(current_time.timestamp())
        
        # Track if anything has changed
        any_changes = False
        
        # Only recalculate these values if needed (when called through API endpoints)
        # We no longer calculate uptime here - it will be done on the client side
        
        # Get guild count - only update if changed
        guild_count = len(bot_instance.guilds)
        if dashboard_data['bot_stats']['guilds'] != guild_count:
            dashboard_data['bot_stats']['guilds'] = guild_count
            data_changed['bot_stats'] = True
            any_changes = True
        
        # Reset global counters for recalculation only when needed
        total_songs_played = 0
        total_queue_length = 0
        active_voice = 0
        
        # Track guild changes
        guild_changes = False
        
        # Gather guild-specific stats
        for guild in bot_instance.guilds:
            guild_id = str(guild.id)
            # Check if we need to initialize server stats
            if guild_id not in dashboard_data['server_stats']:
                dashboard_data['server_stats'][guild_id] = {
                    'name': guild.name,
                    'member_count': guild.member_count,
                    'songs_played': 0,
                    'total_play_time': 0,  # In seconds
                    'song_history': [],    # Server-specific history
                    'top_songs': [],       # Server-specific top songs
                    'last_active': current_time.strftime("%Y-%m-%d %H:%M:%S"),
                    'queue_length': 0,
                    'first_seen': current_time.strftime("%Y-%m-%d %H:%M:%S"),
                    'total_bot_usage_time': 0,  # Total time in voice in seconds
                    'is_currently_in_voice': False,
                    'voice_join_time': None,
                    'most_active_hours': [0] * 24  # Array to track activity by hour
                }
                guild_changes = True
                any_changes = True
            
            # Also maintain the original guild_stats for backward compatibility
            if guild_id not in dashboard_data['guild_stats']:
                dashboard_data['guild_stats'][guild_id] = {
                    'name': guild.name,
                    'member_count': guild.member_count,
                    'songs_played': 0,
                    'queue_length': 0
                }
                guild_changes = True
                any_changes = True
                
            # Check if member count has changed
            if dashboard_data['server_stats'][guild_id]['member_count'] != guild.member_count:
                dashboard_data['server_stats'][guild_id]['member_count'] = guild.member_count
                dashboard_data['guild_stats'][guild_id]['member_count'] = guild.member_count
                guild_changes = True
                any_changes = True
                
            # Check voice client status - only update if there are changes
            old_voice_status = dashboard_data['server_stats'][guild_id]['is_currently_in_voice']
            current_in_voice = guild.voice_client and guild.voice_client.is_connected()
            
            if current_in_voice:
                active_voice += 1
                current_hour = current_time.hour
                
                # Only update if voice status changed
                if not old_voice_status:
                    # Bot just joined voice
                    dashboard_data['server_stats'][guild_id]['is_currently_in_voice'] = True
                    dashboard_data['server_stats'][guild_id]['voice_join_time'] = current_timestamp
                    dashboard_data['server_stats'][guild_id]['last_active'] = current_time.strftime("%Y-%m-%d %H:%M:%S")
                    guild_changes = True
                    any_changes = True
                
                # Track activity by hour
                dashboard_data['server_stats'][guild_id]['most_active_hours'][current_hour] += 1
            else:
                # Bot is not in voice
                if old_voice_status:
                    # Bot just left voice, calculate session duration
                    join_time = dashboard_data['server_stats'][guild_id]['voice_join_time']
                    if join_time:
                        session_duration = current_timestamp - join_time
                        dashboard_data['server_stats'][guild_id]['total_bot_usage_time'] += session_duration
                    
                    # Reset voice tracking
                    dashboard_data['server_stats'][guild_id]['is_currently_in_voice'] = False
                    dashboard_data['server_stats'][guild_id]['voice_join_time'] = None
                    guild_changes = True
                    any_changes = True
                
            # Update queue length for this guild - only if changed
            if hasattr(bot_instance, 'queue_manager'):
                queue = bot_instance.queue_manager.queues.get(guild.id, [])
                queue_length = len(queue)
                
                if dashboard_data['guild_stats'][guild_id]['queue_length'] != queue_length:
                    dashboard_data['guild_stats'][guild_id]['queue_length'] = queue_length
                    dashboard_data['server_stats'][guild_id]['queue_length'] = queue_length
                    total_queue_length += queue_length
                    guild_changes = True
                    any_changes = True
                else:
                    total_queue_length += queue_length
                
                # Get current song if any - only update if changed
                current_song = bot_instance.queue_manager.current_songs.get(guild.id)
                current_song_url = current_song.url if current_song else None
                existing_song_url = dashboard_data['guild_stats'].get(guild_id, {}).get('current_song', {}).get('url')
                
                if current_song_url != existing_song_url:
                    if current_song:
                        # Extract artist and title from the song title (usually in format "Artist - Title")
                        title_parts = current_song.title.split(" - ", 1)
                        artist = title_parts[0] if len(title_parts) > 1 else "Unknown Artist"
                        title = title_parts[1] if len(title_parts) > 1 else current_song.title
                        
                        # We don't calculate progress here anymore - this will be done client-side
                        
                        # Determine if this is from Spotify, YouTube, etc.
                        source = "YouTube"
                        if hasattr(current_song, 'url') and "spotify.com" in current_song.url:
                            source = "Spotify"
                        
                        # Extract duration in seconds for client-side calculation
                        duration_seconds = 0
                        if hasattr(current_song, 'duration') and current_song.duration:
                            duration_parts = current_song.duration.split(':')
                            if len(duration_parts) == 2:
                                try:
                                    duration_seconds = int(duration_parts[0]) * 60 + int(duration_parts[1])
                                except ValueError:
                                    duration_seconds = 0
                        
                        # Set start timestamp for client-side calculations
                        start_timestamp = current_timestamp
                        
                        # Create rich metadata object
                        current_song_data = {
                            'title': title,
                            'artist': artist,
                            'full_title': current_song.title,
                            'url': current_song.url,
                            'duration': current_song.duration,
                            'duration_seconds': duration_seconds,  # Add duration in seconds
                            'thumbnail': current_song.thumbnail,
                            'started_at': current_time.strftime("%Y-%m-%d %H:%M:%S"),
                            'start_time_unix': start_timestamp,
                            'source': source
                        }
                        dashboard_data['guild_stats'][guild_id]['current_song'] = current_song_data
                        dashboard_data['server_stats'][guild_id]['current_song'] = current_song_data
                    else:
                        for stats_dict in [dashboard_data['guild_stats'][guild_id], dashboard_data['server_stats'][guild_id]]:
                            if 'current_song' in stats_dict:
                                stats_dict.pop('current_song')
                    
                    guild_changes = True
                    any_changes = True
            
            # Add to global counters
            total_songs_played += dashboard_data['server_stats'][guild_id]['songs_played']
        
        # Update global stats - only if changed
        if dashboard_data['bot_stats']['active_voice_channels'] != active_voice:
            dashboard_data['bot_stats']['active_voice_channels'] = active_voice
            data_changed['bot_stats'] = True
            any_changes = True
            
        if dashboard_data['bot_stats']['total_songs_played'] != total_songs_played:
            dashboard_data['bot_stats']['total_songs_played'] = total_songs_played
            data_changed['bot_stats'] = True
            any_changes = True
            
        if dashboard_data['bot_stats'].get('total_queue_length', 0) != total_queue_length:
            dashboard_data['bot_stats']['total_queue_length'] = total_queue_length
            data_changed['bot_stats'] = True
            any_changes = True
            
        # Update guild_stats changed flag
        if guild_changes:
            data_changed['guild_stats'] = True
            
        # Update last_updated timestamp if any changes occurred
        if any_changes:
            dashboard_data['bot_stats']['last_updated'] = current_timestamp
        
        logger.debug("Dashboard stats updated successfully")
    except Exception as e:
        logger.error(f"Error updating dashboard stats: {e}")

def record_song_played(guild_id, song):
    """Record information about a played song"""
    global dashboard_data, data_changed
    
    try:
        # Increment total songs played count
        dashboard_data['bot_stats']['total_songs_played'] += 1
        data_changed['bot_stats'] = True
        
        # Process guild-specific statistics
        guild_id_str = str(guild_id)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Get guild name
        guild_name = dashboard_data['guild_stats'].get(guild_id_str, {}).get('name', 'Unknown Server')
        
        # Create song entry
        song_entry = {
            'title': song.title,
            'url': song.url,
            'timestamp': timestamp,
            'timestamp_unix': int(datetime.now().timestamp()),  # Add Unix timestamp
            'guild': guild_name,
            'thumbnail': song.thumbnail,
            'guild_id': guild_id_str
        }
        
        # Update existing guild stats (backward compatibility)
        if guild_id_str in dashboard_data['guild_stats']:
            dashboard_data['guild_stats'][guild_id_str]['songs_played'] += 1
            data_changed['guild_stats'] = True
        
        # Update new per-server stats
        if guild_id_str in dashboard_data['server_stats']:
            server_stats = dashboard_data['server_stats'][guild_id_str]
            
            # Update songs played counter
            server_stats['songs_played'] += 1
            server_stats['last_active'] = timestamp
            
            # Add to server-specific song history
            server_stats['song_history'].insert(0, song_entry.copy())
            server_stats['song_history'] = server_stats['song_history'][:30]  # Keep last 30 per server
            
            # Update server-specific top songs
            song_found = False
            for top_song in server_stats['top_songs']:
                if top_song['url'] == song.url:
                    top_song['play_count'] += 1
                    song_found = True
                    break
                    
            if not song_found:
                server_stats['top_songs'].append({
                    'title': song.title,
                    'url': song.url,
                    'thumbnail': song.thumbnail,
                    'play_count': 1
                })
                
            # Sort server top songs by play count
            server_stats['top_songs'] = sorted(
                server_stats['top_songs'], 
                key=lambda x: x['play_count'], 
                reverse=True
            )[:15]  # Keep only top 15 songs per server
        
        # Add to global history, keeping most recent 8
        dashboard_data['song_history'].insert(0, song_entry)
        dashboard_data['song_history'] = dashboard_data['song_history'][:8]
        data_changed['song_history'] = True
        
        # Update global top songs
        song_found = False
        for top_song in dashboard_data['top_songs']:
            if top_song['url'] == song.url:
                top_song['play_count'] += 1
                song_found = True
                break
                
        if not song_found:
            dashboard_data['top_songs'].append({
                'title': song.title,
                'url': song.url,
                'thumbnail': song.thumbnail,
                'play_count': 1
            })
            
        # Sort global top songs by play count
        dashboard_data['top_songs'] = sorted(
            dashboard_data['top_songs'], 
            key=lambda x: x['play_count'], 
            reverse=True
        )[:8]  # Keep only top 8 songs
        
        # Update last_updated timestamp
        dashboard_data['bot_stats']['last_updated'] = int(datetime.now().timestamp())
        
        logger.debug(f"Recorded song: {song.title}")
    except Exception as e:
        logger.error(f"Error recording song play: {e}")

# Save and load dashboard data
def save_dashboard_data():
    """Save dashboard data to disk"""
    try:
        # Create a serializable copy of the data, preserving all important metrics
        data_to_save = {
            'bot_stats': {
                'total_songs_played': dashboard_data['bot_stats']['total_songs_played'],
                'uptime': dashboard_data['bot_stats']['uptime'],
                'guilds': dashboard_data['bot_stats']['guilds'],
                'active_voice_channels': dashboard_data['bot_stats']['active_voice_channels'],
                # Save the start time as a string that can be parsed later
                'start_time': dashboard_data['bot_stats']['start_time'].strftime("%Y-%m-%d %H:%M:%S")
            },
            'song_history': dashboard_data['song_history'],
            'top_songs': dashboard_data['top_songs'],
            'guild_stats': dashboard_data['guild_stats'],
            'server_stats': {}
        }
        
        # Process server stats (include everything except transient data)
        for guild_id, stats in dashboard_data['server_stats'].items():
            # Create a copy of the stats that can be serialized
            server_data = {
                'name': stats.get('name', 'Unknown Server'),
                'member_count': stats.get('member_count', 0),
                'songs_played': stats.get('songs_played', 0),
                'total_play_time': stats.get('total_play_time', 0),
                'song_history': stats.get('song_history', []),
                'top_songs': stats.get('top_songs', []),
                'last_active': stats.get('last_active', ''),
                'queue_length': stats.get('queue_length', 0),
                'first_seen': stats.get('first_seen', ''),
                'total_bot_usage_time': stats.get('total_bot_usage_time', 0),
                'most_active_hours': stats.get('most_active_hours', [0] * 24)
            }
            
            # Skip transient data like is_currently_in_voice, voice_join_time, and current_song
            # These will be reinitialized when the bot reconnects
            
            data_to_save['server_stats'][guild_id] = server_data
        
        # Save to file with pretty indentation
        os.makedirs('data', exist_ok=True)
        with open('data/dashboard_data.json', 'w') as f:
            json.dump(data_to_save, f, indent=2)
        logger.info("Dashboard data saved successfully")
    except Exception as e:
        logger.error(f"Error saving dashboard data: {e}")

def load_dashboard_data():
    """Load dashboard data from disk"""
    global dashboard_data
    try:
        if os.path.exists('data/dashboard_data.json'):
            with open('data/dashboard_data.json', 'r') as f:
                loaded_data = json.load(f)
                
            # Update the bot stats
            if 'bot_stats' in loaded_data:
                # Keep the current start time, but restore the total_songs_played count
                dashboard_data['bot_stats']['total_songs_played'] = loaded_data['bot_stats'].get('total_songs_played', 0)
                dashboard_data['bot_stats']['guilds'] = loaded_data['bot_stats'].get('guilds', 0)
                
                # Convert the start_time string back to datetime if present
                if 'start_time' in loaded_data['bot_stats']:
                    try:
                        saved_start_time = datetime.strptime(loaded_data['bot_stats']['start_time'], "%Y-%m-%d %H:%M:%S")
                        # Calculate how long the bot was down
                        downtime = (datetime.now() - saved_start_time)
                        logger.info(f"Bot was down for: {str(downtime).split('.')[0]}")
                    except Exception as e:
                        logger.error(f"Error parsing start_time: {e}")
            
            # Restore song history
            if 'song_history' in loaded_data:
                dashboard_data['song_history'] = loaded_data['song_history']
                
            # Restore top songs
            if 'top_songs' in loaded_data:
                dashboard_data['top_songs'] = loaded_data['top_songs']
                
            # Restore guild stats (legacy)
            if 'guild_stats' in loaded_data:
                dashboard_data['guild_stats'] = loaded_data['guild_stats']
                
            # Restore detailed server stats
            if 'server_stats' in loaded_data:
                # For each server in the loaded data
                for guild_id, stats in loaded_data['server_stats'].items():
                    # Initialize the entry if it doesn't exist
                    if guild_id not in dashboard_data['server_stats']:
                        dashboard_data['server_stats'][guild_id] = {
                            'name': stats.get('name', 'Unknown Server'),
                            'member_count': stats.get('member_count', 0),
                            'songs_played': 0,
                            'total_play_time': 0,
                            'song_history': [],
                            'top_songs': [],
                            'last_active': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            'queue_length': 0,
                            'first_seen': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            'total_bot_usage_time': 0,
                            'is_currently_in_voice': False,
                            'voice_join_time': None,
                            'most_active_hours': [0] * 24
                        }
                    
                    # Update with loaded data
                    server_stats = dashboard_data['server_stats'][guild_id]
                    server_stats['songs_played'] = stats.get('songs_played', 0)
                    server_stats['total_play_time'] = stats.get('total_play_time', 0)
                    server_stats['song_history'] = stats.get('song_history', [])
                    server_stats['top_songs'] = stats.get('top_songs', [])
                    server_stats['last_active'] = stats.get('last_active', server_stats['last_active'])
                    server_stats['first_seen'] = stats.get('first_seen', server_stats['first_seen'])
                    server_stats['total_bot_usage_time'] = stats.get('total_bot_usage_time', 0)
                    server_stats['most_active_hours'] = stats.get('most_active_hours', [0] * 24)
                
            logger.info("Dashboard data loaded successfully")
    except Exception as e:
        logger.error(f"Error loading dashboard data: {e}")

# Routes
@app.route(f'{URL_PREFIX}/ping')
def ping():
    """Simple test endpoint to verify the server is working"""
    return jsonify({
        "status": "ok",
        "message": "Dashboard is running",
        "timestamp": datetime.now().isoformat()
    })

# Route for static files
@app.route(f'{URL_PREFIX}/static/<path:filename>')
def serve_static(filename):
    """Serve static files with the correct URL prefix"""
    try:
        logger.debug(f"Serving static file: {filename}")
        return app.send_static_file(filename)
    except Exception as e:
        logger.error(f"Error serving static file {filename}: {e}")
        return f"Error serving static file: {str(e)}", 500

# Handle root-level static file requests
@app.route('/favicon.ico')
def favicon():
    """Serve the favicon"""
    try:
        logger.debug("Serving favicon.ico")
        return app.send_static_file('favicon.ico')
    except Exception as e:
        logger.error(f"Error serving favicon.ico: {e}")
        # Return a 204 No Content instead of an error for favicon
        return '', 204

# Handle other common root static files
@app.route('/<path:filename>')
def serve_root_static(filename):
    """Serve certain files from the root URL path (no prefix)"""
    try:
        # Only allow common web files at the root
        allowed_root_files = [
            'robots.txt',
            'sitemap.xml',
            'manifest.json',
            'sw.js',  # Service worker
            'browserconfig.xml'
        ]
        if filename in allowed_root_files:
            logger.debug(f"Serving root static file: {filename}")
            return app.send_static_file(filename)
        else:
            logger.debug(f"Redirecting non-static file request: {filename}")
            return redirect(f"{URL_PREFIX}/")
    except Exception as e:
        logger.error(f"Error serving root static file {filename}: {e}")
        # Return a 204 No Content for better browser handling
        return '', 204

@app.route(f'{URL_PREFIX}/')
def home():
    """Render the home dashboard"""
    try:
        # Just send the initial data without full recalculation
        return render_template('dashboard.html', data=dashboard_data)
    except Exception as e:
        logger.error(f"Error rendering home template: {e}", exc_info=True)
        return jsonify({"error": "Template rendering error", "details": str(e)}), 500

@app.route(f'{URL_PREFIX}/api/stats')
def get_stats():
    # Check if client provided last-updated timestamp
    client_last_updated = request.args.get('last_updated', 0, type=int)
    server_last_updated = dashboard_data['bot_stats'].get('last_updated', 0)
    
    # Only recalculate if client data is older than server data
    if client_last_updated < server_last_updated:
        update_stats()
        
        # Create a response with only changed data
        response_data = {
            'last_updated': server_last_updated
        }
        
        # Only include data that has changed
        if data_changed['bot_stats']:
            response_data['bot_stats'] = dashboard_data['bot_stats']
            data_changed['bot_stats'] = False
            
        if data_changed['song_history']:
            response_data['song_history'] = dashboard_data['song_history']
            data_changed['song_history'] = False
            
        if data_changed['guild_stats']:
            response_data['guild_stats'] = dashboard_data['guild_stats']
            data_changed['guild_stats'] = False
            
        # Reset flags after sending
        for key in data_changed:
            data_changed[key] = False
            
        return jsonify(response_data)
    else:
        # No changes, return minimal response
        return jsonify({'last_updated': server_last_updated, 'no_changes': True})

@app.route(f'{URL_PREFIX}/api/guilds')
def get_guilds():
    # Check if client provided last-updated timestamp
    client_last_updated = request.args.get('last_updated', 0, type=int)
    server_last_updated = dashboard_data['bot_stats'].get('last_updated', 0)
    
    # Only update if client data is older
    if client_last_updated < server_last_updated and data_changed['guild_stats']:
        update_stats()
        data_changed['guild_stats'] = False
        return jsonify(dashboard_data['guild_stats'])
    else:
        # No changes
        return jsonify({'no_changes': True})

@app.route(f'{URL_PREFIX}/guild/<guild_id>')
def guild_detail(guild_id):
    # Only update stats if this guild's data has changed
    guild_data = dashboard_data['guild_stats'].get(guild_id, {})
    return render_template('guild.html', guild=guild_data, guild_id=guild_id)

@app.route(f'{URL_PREFIX}/api/history')
def get_history():
    # Check if client provided last-updated timestamp
    client_last_updated = request.args.get('last_updated', 0, type=int)
    server_last_updated = dashboard_data['bot_stats'].get('last_updated', 0)
    
    # Only update if client data is older
    if client_last_updated < server_last_updated and data_changed['song_history']:
        data_changed['song_history'] = False
        return jsonify(dashboard_data['song_history'])
    else:
        # No changes
        return jsonify({'no_changes': True})

# Auto save data every 5 minutes
def auto_save_task():
    """Periodically save dashboard data to disk"""
    import time
    while True:
        time.sleep(300)  # 5 minutes
        save_dashboard_data()

# Start the dashboard server
def start_dashboard(host='0.0.0.0', port=8080, url_prefix='/musho', debug=False):
    """Start the dashboard web server in a separate thread"""
    # First load any saved data
    load_dashboard_data()
    
    # Start auto-save in a daemon thread
    save_thread = threading.Thread(target=auto_save_task, daemon=True)
    save_thread.start()
    
    # Create logs directory if it doesn't exist
    os.makedirs('logs', exist_ok=True)
    
    try:
        # Log the network binding configuration
        logger.info(f"Flask app configured to bind to: {host}:{port}")
        
        # Simplify by running Flask directly without middleware
        def run_app():
            try:
                logger.info(f"Starting dashboard server on 0.0.0.0:{port} with routes at {url_prefix}")
                # Force app to bind to 0.0.0.0 - this is the key fix for "Connection reset by peer"
                app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
            except Exception as e:
                logger.error(f"Error starting dashboard server: {e}", exc_info=True)
            
        dashboard_thread = threading.Thread(target=run_app, daemon=True)
        dashboard_thread.start()
        logger.info(f"Dashboard started on http://0.0.0.0:{port}{url_prefix}/")
        
        return dashboard_thread
    except Exception as e:
        logger.error(f"Failed to start dashboard: {e}", exc_info=True)
        return None

# For testing directly
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=True)
