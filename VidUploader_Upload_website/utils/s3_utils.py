import logging
import mimetypes
import boto3
from botocore.exceptions import ClientError
from config import AWS_CONFIG

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def get_s3_client():
    """Get an S3 client instance"""
    return boto3.client(
        's3',
        region_name=AWS_CONFIG["S3_REGION_NAME"],
        aws_access_key_id=AWS_CONFIG["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=AWS_CONFIG["AWS_SECRET_ACCESS_KEY"]
    )

def get_content_type(filename):
    """Get the content type for a file."""
    extension = filename.lower().split('.')[-1]
    content_types = {
        'mp4': 'video/mp4',
        'mov': 'video/quicktime',
        'webm': 'video/webm',
        'html': 'text/html',
        'jpg': 'image/jpeg',
        'jpeg': 'image/jpeg',
        'png': 'image/png'
    }
    return content_types.get(extension, 'application/octet-stream')

def upload_to_s3(file_obj, object_name):
    """
    Upload a file to S3 bucket
    
    Parameters:
    - file_obj: File object to upload
    - object_name: S3 object name (key)
    
    Returns:
    - str: Public URL of the uploaded file if successful, None otherwise
    """
    try:
        s3_client = get_s3_client()
        content_type = get_content_type(object_name)
        extra_args = {
            "ContentType": content_type,
            "ContentDisposition": 'inline',
            "CacheControl": "max-age=86400"  # 24 hour cache
        }

        # Upload the file
        s3_client.upload_fileobj(
            file_obj,
            AWS_CONFIG["S3_BUCKET_NAME"],
            object_name,
            ExtraArgs=extra_args
        )

        # Generate URL with correct regional endpoint
        url = f"https://{AWS_CONFIG['S3_BUCKET_NAME']}.s3-{AWS_CONFIG['S3_REGION_NAME']}.amazonaws.com/{object_name}"
        return url

    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', '')
        error_msg = e.response.get('Error', {}).get('Message', '')
        logger.error(f"S3 ClientError uploading '{object_name}': {error_code} - {error_msg}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error uploading '{object_name}': {str(e)}")
        return None

def delete_from_s3(object_name):
    """Delete a file from S3 bucket"""
    try:
        s3_client = get_s3_client()
        s3_client.delete_object(
            Bucket=AWS_CONFIG["S3_BUCKET_NAME"],
            Key=object_name
        )
        return True
    except Exception as e:
        logger.error(f"Error deleting '{object_name}' from S3: {str(e)}")
        return False
