import os
import sqlite3
import random
import string
from datetime import datetime, timedelta, UTC

import requests
from flask import Flask, request, redirect, session, render_template, jsonify, flash, url_for, g
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import ffmpeg
from functools import wraps
import bleach
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import secrets
from utils.s3_utils import upload_to_s3, delete_from_s3
from config import AWS_CONFIG
import magic
from werkzeug.middleware.proxy_fix import ProxyFix

def adapt_datetime(dt):
    """Convert datetime to ISO format string."""
    return dt.isoformat()

def convert_datetime(s):
    """Convert ISO format string to datetime with UTC timezone."""
    try:
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
    except ValueError:
        return datetime.strptime(s, '%Y-%m-%d %H:%M:%S.%f').replace(tzinfo=UTC)

sqlite3.register_adapter(datetime, adapt_datetime)
sqlite3.register_converter("timestamp", convert_datetime)

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY")

# Database
DATABASE = "videos.db"

# Upload configurations
UPLOAD_FOLDER = "static/uploads"  # Changed to uploads for clarity
ALLOWED_EXTENSIONS = {"mp4", "mov", "webm"}  # Common video extensions

# Discord OAuth
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")

# Ensure upload directory exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

csrf = CSRFProtect(app)

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["4000 per day", "50 per hour"],
    storage_uri="memory://"
)

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

app.config.update(
    MAX_CONTENT_LENGTH = 1000 * 1024 * 1024,  # 1000MB max-size
    UPLOAD_RATE_LIMIT = "10 per minute",
    LOGIN_RATE_LIMIT = "5 per minute",
    ALLOWED_MIME_TYPES = ['video/mp4', 'video/webm', 'video/quicktime'],
)

def init_db():
    with sqlite3.connect(DATABASE) as conn:
        c = conn.cursor()
        
        # Create videos table with additional columns for metadata and future features
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL UNIQUE,
                title TEXT,
                description TEXT,  -- For future video descriptions
                upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                user_id TEXT,
                length INTEGER,  -- Video length in seconds
                fps INTEGER,     -- Frames per second
                resolution TEXT, -- Video resolution
                thumbnail TEXT,  -- Path to the thumbnail image
                likes INTEGER DEFAULT 0,  -- For future likes/reactions
                filesize INTEGER,  -- File size in bytes
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        
        # Add indexes for frequently queried fields
        c.execute("CREATE INDEX IF NOT EXISTS idx_videos_user_id ON videos(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_videos_upload_date ON videos(upload_date)")

        # Create users table
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT,
                avatar_url TEXT
            )
            """
        )

        # Create user_sessions table
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS user_sessions (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        
        # Add index for user sessions
        c.execute("CREATE INDEX IF NOT EXISTS idx_user_sessions_user_id ON user_sessions(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_user_sessions_expires_at ON user_sessions(expires_at)")

        # Create comments table for future implementation
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id INTEGER,
                user_id TEXT,
                comment TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (video_id) REFERENCES videos(id),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        
        # Add index for comments
        c.execute("CREATE INDEX IF NOT EXISTS idx_comments_video_id ON comments(video_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_comments_user_id ON comments(user_id)")

        conn.commit()


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_user(user_id):
    with sqlite3.connect(DATABASE) as conn:
        c = conn.cursor()
        c.execute("SELECT id, username, avatar_url FROM users WHERE id = ?", (user_id,))
        user = c.fetchone()
        if user:
            return {
                'id': user[0],
                'username': user[1],
                'avatar_url': user[2]
            }
    return None

def get_s3_url(filename, prefix=""):
    """Generate S3 URL for a file."""
    if not filename:
        return None
    return f"https://{AWS_CONFIG['S3_BUCKET_NAME']}.s3-{AWS_CONFIG['S3_REGION_NAME']}.amazonaws.com/{prefix}{filename}"

@app.route("/")
def index():
    user = None
    page = request.args.get('page', 1, type=int)
    per_page = 20  # Number of videos per page
    
    if 'user_id' in session:
        user = get_user(session['user_id'])
        
    try:
        conn = get_db()
        c = conn.cursor()
        
        # Get total count for pagination
        c.execute("SELECT COUNT(*) FROM videos")
        total_videos = c.fetchone()[0]
        
        # Get paginated videos
        c.execute(
            """
            SELECT v.id, v.filename, v.title, 
                   strftime('%Y-%m-%d', v.upload_date) as upload_date, 
                   u.username, v.length, v.fps, v.resolution, 
                   v.thumbnail, v.filesize, v.description
            FROM videos v
            LEFT JOIN users u ON v.user_id = u.id
            ORDER BY v.upload_date DESC
            LIMIT ? OFFSET ?
            """,
            (per_page, (page - 1) * per_page)
        )
        videos = list(c.fetchall())
        
        # Calculate total pages
        total_pages = (total_videos + per_page - 1) // per_page
        
        # Return JSON if requested (for infinite scroll)
        if request.args.get('format') == 'json':
            return jsonify({
                'videos': videos,
                'total_pages': total_pages,
                'current_page': page,
                'aws_config': AWS_CONFIG
            })
        
        return render_template("index.html", 
                              videos=videos, 
                              user=user, 
                              aws_config=AWS_CONFIG,
                              page=page,
                              total_pages=total_pages)
    except Exception as e:
        app.logger.error(f"Database error in index route: {str(e)}")
        flash('Error loading videos', 'error')
        
        # Return JSON error if requested
        if request.args.get('format') == 'json':
            return jsonify({
                'error': 'Error loading videos',
                'videos': [],
                'total_pages': 1,
                'current_page': page
            }), 500
            
        return render_template("index.html", 
                              videos=[], 
                              user=user, 
                              aws_config=AWS_CONFIG,
                              page=1,
                              total_pages=1)


def extract_video_metadata(filepath):
    try:
        probe = ffmpeg.probe(filepath)
        video_stream = next((stream for stream in probe['streams'] if stream['codec_type'] == 'video'), None)
        
        if video_stream is None:
            raise ValueError("No video stream found")

        metadata = {
            "length": float(probe['format']['duration']),
            "fps": eval(video_stream['r_frame_rate']),  # Convert frame rate to float
            "resolution": f"{video_stream['width']}x{video_stream['height']}"
        }
        print(f"Extracted metadata: {metadata}")
        return metadata
    except ffmpeg.Error as e:
        print(f"Error extracting metadata: {e.stderr.decode()}")
        return {"length": 0, "fps": 0, "resolution": "Unknown"}

def generate_thumbnail(filepath, thumbnail_path):
    try:
        (
            ffmpeg
            .input(filepath, ss=1)  # Capture a frame at 1 second
            .output(thumbnail_path, vframes=1)
            .run(capture_stdout=True, capture_stderr=True)
        )
        print(f"Thumbnail generated at: {thumbnail_path}")
    except ffmpeg.Error as e:
        print(f"Error generating thumbnail: {e.stderr.decode()}")

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please login to access this feature', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route("/upload", methods=["POST"])
@limiter.limit(app.config['UPLOAD_RATE_LIMIT'])
@login_required
def upload():
    try:
        # Verify CSRF token except for file uploads in specific circumstances
        if request.content_type and not request.content_type.startswith('multipart/form-data'):
            csrf.protect()  # Only check CSRF for non-multipart/form-data

        if not request.files or "video" not in request.files:
            return jsonify({"error": "No video file provided"}), 400

        file = request.files["video"]
        if file.filename == "":
            return jsonify({"error": "No file selected"}), 400
            
        # Enhanced file validation
        if not allowed_file(file.filename):
            return jsonify({"error": "Invalid file type"}), 400

        # Sanitize and validate title with length limit
        title = bleach.clean(request.form.get("title", "Untitled Video"))[:100]
        if not title.strip():
            title = "Untitled Video"
            
        # Sanitize and validate description with length limit
        description = bleach.clean(request.form.get("description", ""))[:5000]

        # Generate secure filename with hash
        filename = secure_filename_with_hash(file.filename)
        
        # Create a temporary file to validate the video
        temp_filepath = os.path.join("/tmp", filename)
        try:
            file.save(temp_filepath)
            
            # Validate file size
            file_size = os.path.getsize(temp_filepath)
            if file_size > app.config['MAX_CONTENT_LENGTH']:
                os.remove(temp_filepath)
                return jsonify({"error": "File too large"}), 400

            # Validate actual file type
            if not validate_file_type(temp_filepath):
                os.remove(temp_filepath)
                return jsonify({"error": "Invalid file type"}), 400

            # Get file size in bytes
            filesize = os.path.getsize(temp_filepath)

            # Validate that it's actually a video file
            if not is_valid_video(temp_filepath):
                os.remove(temp_filepath)
                return jsonify({"error": "Invalid video file"}), 400

            metadata = extract_video_metadata(temp_filepath)
            
            # Generate thumbnail
            thumbnail_filename = f"thumb_{filename}.jpg"
            thumbnail_path = os.path.join("/tmp", thumbnail_filename)
            generate_thumbnail(temp_filepath, thumbnail_path)
            
            # Upload video to S3
            with open(temp_filepath, 'rb') as video_file:
                video_url = upload_to_s3(video_file, f"videos/{filename}")
            
            if not video_url:
                raise Exception("Failed to upload video to S3")
                
            # Upload thumbnail to S3
            with open(thumbnail_path, 'rb') as thumb_file:
                thumbnail_url = upload_to_s3(thumb_file, f"thumbnails/{thumbnail_filename}")
                
            if not thumbnail_url:
                raise Exception("Failed to upload thumbnail to S3")

            # Clean up temporary files
            os.remove(temp_filepath)
            os.remove(thumbnail_path)

            with sqlite3.connect(DATABASE) as conn:
                c = conn.cursor()
                c.execute(
                    """
                    INSERT INTO videos (filename, title, description, user_id, length, fps, resolution, thumbnail, filesize)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (filename, title, description, session['user_id'], metadata['length'], 
                    metadata['fps'], metadata['resolution'], thumbnail_filename, filesize)
                )
                conn.commit()
            
            return jsonify({"success": True}), 200
            
        except Exception as e:
            if os.path.exists(temp_filepath):
                os.remove(temp_filepath)
            raise

        finally:
            # Ensure temporary file is cleaned up
            if os.path.exists(temp_filepath):
                try:
                    os.remove(temp_filepath)
                except:
                    pass

    except Exception as e:
        app.logger.error(f"Upload error: {str(e)}", exc_info=True)
        return jsonify({"error": "Upload failed"}), 500

@app.route("/delete_video/<int:video_id>", methods=["POST"])
@login_required
def delete_video(video_id):
    try:
        # Get the video and validate ownership
        with sqlite3.connect(DATABASE) as conn:
            c = conn.cursor()
            c.execute(
                """
                SELECT id, filename, thumbnail, user_id FROM videos WHERE id = ?
                """,
                (video_id,)
            )
            video = c.fetchone()
            
            if not video:
                flash("Video not found", "danger")
                return redirect(url_for("library"))
                
            # Ensure the user can only delete their own videos
            if video[3] != session['user_id']:
                flash("You don't have permission to delete this video", "danger")
                return redirect(url_for("library"))
                
            # Delete from S3
            if video[1]:  # Video filename
                delete_from_s3(f"videos/{video[1]}")
                
            if video[2]:  # Thumbnail filename
                delete_from_s3(f"thumbnails/{video[2]}")
                
            # Delete from database
            c.execute("DELETE FROM videos WHERE id = ?", (video_id,))
            conn.commit()
            
            flash("Video deleted successfully", "success")
            return redirect(url_for("library"))
            
    except Exception as e:
        print(f"Error deleting video: {str(e)}")
        flash("An error occurred while deleting the video", "danger")
        return redirect(url_for("library"))

@app.route('/login')
def login():
    # Generate and store state parameter
    state = ''.join(random.choices(string.ascii_letters + string.digits, k=32))
    session['oauth_state'] = state
    
    return redirect(
        f'https://discord.com/api/oauth2/authorize'
        f'?client_id={DISCORD_CLIENT_ID}'
        f'&redirect_uri={DISCORD_REDIRECT_URI}'
        f'&response_type=code'
        f'&state={state}'
        f'&scope=identify'
        f'&prompt=consent'
    )


@app.route('/callback')
def callback():
    # Verify state parameter
    stored_state = session.get('oauth_state')
    state = request.args.get('state')
    if not stored_state or stored_state != state:
        return 'State verification failed', 403

    code = request.args.get('code')
    if not code:
        return redirect('/login')

    # Exchange code for token
    data = {
        'client_id': DISCORD_CLIENT_ID,
        'client_secret': DISCORD_CLIENT_SECRET,
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': DISCORD_REDIRECT_URI,
        'scope': 'identify'
    }
    
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded'
    }

    # Get access token
    response = requests.post('https://discord.com/api/oauth2/token', data=data, headers=headers)
    if response.status_code != 200:
        return 'Failed to get access token', 500
    
    token_data = response.json()
    access_token = token_data['access_token']

    # Get user data
    user_response = requests.get(
        'https://discord.com/api/users/@me',
        headers={'Authorization': f'Bearer {access_token}'}
    )
    
    if user_response.status_code != 200:
        return 'Failed to get user info', 500

    user_data = user_response.json()
    
    # Get avatar URL
    avatar_hash = user_data.get('avatar')
    avatar_url = (
        f"https://cdn.discordapp.com/avatars/{user_data['id']}/{avatar_hash}.png"
        if avatar_hash else
        "https://cdn.discordapp.com/embed/avatars/0.png"
    )

    # Generate a secure session token
    session_token = secrets.token_urlsafe(32)
    session_expiry = datetime.now(UTC) + timedelta(days=30)  # 30 days expiry

    # Store in database
    with sqlite3.connect(DATABASE) as conn:
        c = conn.cursor()
        # Add sessions table if it doesn't exist
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_sessions (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        
        # Store user info
        c.execute(
            "INSERT OR REPLACE INTO users (id, username, avatar_url) VALUES (?, ?, ?)",
            (user_data['id'], user_data['username'], avatar_url)
        )
        
        # Store session token
        c.execute(
            "INSERT INTO user_sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
            (session_token, user_data['id'], session_expiry)
        )
        conn.commit()

    # Set secure cookie with session token
    response = redirect('/')
    response.set_cookie(
        'session_token',
        session_token,
        httponly=True,
        secure=True,  # Enable in production with HTTPS
        samesite='Lax',
        expires=session_expiry,
        max_age=30 * 24 * 60 * 60  # 30 days in seconds
    )
    
    session['user_id'] = user_data['id']
    return response

@app.before_request
def load_user_from_cookie():
    if 'user_id' not in session:
        session_token = request.cookies.get('session_token')
        if session_token:
            with sqlite3.connect(DATABASE) as conn:
                c = conn.cursor()
                c.execute("""
                    SELECT user_id, expires_at 
                    FROM user_sessions 
                    WHERE token = ?
                """, (session_token,))
                result = c.fetchone()
                
                if result:
                    user_id, expires_at = result
                    expires_at = datetime.strptime(expires_at, '%Y-%m-%d %H:%M:%S.%f').replace(tzinfo=UTC)
                    
                    if expires_at > datetime.now(UTC):
                        session['user_id'] = user_id
                    else:
                        # Clean up expired session
                        c.execute("DELETE FROM user_sessions WHERE token = ?", (session_token,))
                        conn.commit()

@app.route('/logout')
def logout():
    # Clear session token from database
    session_token = request.cookies.get('session_token')
    if session_token:
        with sqlite3.connect(DATABASE) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM user_sessions WHERE token = ?", (session_token,))
            conn.commit()
    
    # Clear session and cookie
    session.clear()
    response = redirect('/')
    response.delete_cookie('session_token')
    return response

# Add periodic cleanup of expired sessions
def cleanup_expired_sessions():
    try:
        with sqlite3.connect(DATABASE) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM user_sessions WHERE expires_at < ?", (datetime.now(UTC),))
            conn.commit()
            app.logger.info(f"Cleaned up {c.rowcount} expired sessions")
    except Exception as e:
        app.logger.error(f"Error during session cleanup: {str(e)}")

def is_valid_video(filepath):
    try:
        probe = ffmpeg.probe(filepath)
        # Check if file has at least one video stream
        return any(stream["codec_type"] == "video" for stream in probe["streams"])
    except:
        return False

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(
            DATABASE,
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
            uri=True
        )
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_db(error):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

@app.after_request
def add_security_headers(response):
    """Add security headers to response"""
    s3_domain = f"{AWS_CONFIG['S3_BUCKET_NAME']}.s3-{AWS_CONFIG['S3_REGION_NAME']}.amazonaws.com"
    
    # Enhance Content Security Policy
    response.headers['Content-Security-Policy'] = (
        "default-src 'self' https:; "
        "script-src 'self' 'unsafe-inline' cdn.jsdelivr.net cdnjs.cloudflare.com; "
        f"img-src 'self' https: {s3_domain} cdn.discordapp.com data:; "
        f"media-src 'self' https: {s3_domain} blob:; "
        "style-src 'self' 'unsafe-inline' cdnjs.cloudflare.com; "
        "font-src 'self' cdnjs.cloudflare.com; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self';"
    )
    
    # Set CORS headers for all responses
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS, POST'
    response.headers['Access-Control-Allow-Headers'] = 'Origin, Content-Type, Accept, Range, X-Requested-With'
    
    # Additional security headers
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    
    return response

app.config.update(
    SESSION_COOKIE_SECURE=False,  # Since we're using HTTP
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)

@app.route("/library")
@login_required
def library():
    user = get_user(session['user_id'])
    page = request.args.get('page', 1, type=int)
    per_page = 20  # Number of videos per page
    
    try:
        conn = get_db()
        c = conn.cursor()
        
        # Get total count for pagination
        c.execute("SELECT COUNT(*) FROM videos WHERE user_id = ?", (session['user_id'],))
        total_videos = c.fetchone()[0]
        
        # Get paginated videos
        c.execute(
            """
            SELECT v.id, v.filename, v.title, 
                   strftime('%Y-%m-%d', v.upload_date) as upload_date, 
                   u.username, v.length, v.fps, v.resolution, 
                   v.thumbnail, v.filesize, v.description
            FROM videos v
            LEFT JOIN users u ON v.user_id = u.id
            WHERE v.user_id = ?
            ORDER BY v.upload_date DESC
            LIMIT ? OFFSET ?
            """,
            (session['user_id'], per_page, (page - 1) * per_page)
        )
        videos = c.fetchall()
        
        # Calculate total pages
        total_pages = (total_videos + per_page - 1) // per_page
        
        return render_template("library.html", 
                              videos=videos, 
                              user=user, 
                              aws_config=AWS_CONFIG,
                              page=page,
                              total_pages=total_pages)
    except Exception as e:
        app.logger.error(f"Database error in library route: {str(e)}")
        flash('Error loading videos', 'error')
        return render_template("library.html", 
                              videos=[], 
                              user=user, 
                              aws_config=AWS_CONFIG,
                              page=1,
                              total_pages=1)

def migrate_db():
    """Ensure all required columns exist in the database."""
    with sqlite3.connect(DATABASE) as conn:
        c = conn.cursor()
        
        # Check if description column exists
        c.execute("PRAGMA table_info(videos)")
        columns = [column[1] for column in c.fetchall()]
        
        # Add description column if it doesn't exist
        if 'description' not in columns:
            try:
                c.execute("ALTER TABLE videos ADD COLUMN description TEXT")
                conn.commit()
                print("Added description column to videos table")
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    raise

def init_app(app):
    init_db()
    migrate_db()  # Ensure all columns exist
    app.teardown_appcontext(close_db)
    app.after_request(add_security_headers)

# Add periodic cleanup of expired sessions and improve error handling
@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404

@app.errorhandler(500)
def server_error(e):
    app.logger.error(f"Server error: {str(e)}")
    return render_template('500.html'), 500

def validate_file_type(file_path):
    """Validate file type using libmagic"""
    mime = magic.Magic(mime=True)
    file_type = mime.from_file(file_path)
    return file_type in app.config['ALLOWED_MIME_TYPES']

def secure_filename_with_hash(filename):
    """Generate a secure filename with hash to prevent duplicates"""
    name, ext = os.path.splitext(secure_filename(filename))
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    random_hex = secrets.token_hex(8)
    return f"{timestamp}_{random_hex}_{name[-30:]}{ext}"  # Limit original name to 30 chars

@app.before_request
def enforce_https():
    """Enforce HTTPS in production"""
    if not app.debug and not request.is_secure:
        url = request.url.replace('http://', 'https://', 1)
        return redirect(url, code=301)

if __name__ == '__main__':
    init_app(app)
    app.run(debug=True,
            host='0.0.0.0',
            port=80,
            use_reloader=True)