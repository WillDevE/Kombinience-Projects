import os
from dotenv import load_dotenv

load_dotenv()

# AWS Configuration
AWS_CONFIG = {
    "AWS_ACCESS_KEY_ID": os.getenv("AWS_ACCESS_KEY_ID"),
    "AWS_SECRET_ACCESS_KEY": os.getenv("AWS_SECRET_ACCESS_KEY"),
    "S3_BUCKET_NAME": os.getenv("S3_BUCKET_NAME"),
    "S3_REGION_NAME": os.getenv("AWS_REGION", "ap-southeast-1"),  # Updated default region
}

# Upload configurations
UPLOAD_FOLDER = "static/uploads"  # Kept for thumbnails
ALLOWED_EXTENSIONS = {"mp4", "mov", "webm"}  # Common video extensions
MAX_CONTENT_LENGTH = 200 * 1024 * 1024  # 200MB max file size
