import subprocess
import os
import signal
import sys
import time
import logging
import requests
import tempfile
import threading
import json
from typing import Optional, Dict
from datetime import datetime, timedelta
from pathlib import Path
from django.conf import settings
from django.apps import apps
from django.core.cache import cache
from django.db import transaction
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from googleapiclient.errors import HttpError
from celery import shared_task
import io

logger = logging.getLogger(__name__)

# ============ CONFIGURATION ============
TEMP_DIR = getattr(settings, 'STREAM_TEMP_DIR', '/var/tmp/streams')
MAX_CONCURRENT_DOWNLOADS = getattr(settings, 'MAX_CONCURRENT_DOWNLOADS', 3)
CHUNK_SIZE = 512 * 1024  # 512KB optimal for S3
STREAM_BUFFER_SIZE = '50M'
FFMPEG_TIMEOUT = 300  # 5min per operation
MAX_STREAM_RESTARTS = 5
BROADCAST_END_TIMEOUT = 30  # Wait max 30s for YouTube to acknowledge end
GRACEFUL_SHUTDOWN_TIMEOUT = 10  # Time to gracefully stop FFmpeg
CELERY_TASK_TIMEOUT = 86400  # 24 hours

# Ensure temp directory exists
Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)


# ============ UTILITIES ============

class StreamCache:
    """Redis-backed cache for stream metadata"""
    
    @staticmethod
    def get_stream_key(stream_id):
        return f"stream:{stream_id}"
    
    @staticmethod
    def set_process_info(stream_id, pid, status):
        """Store process info in cache"""
        cache.set(
            StreamCache.get_stream_key(stream_id),
            {
                'pid': pid,
                'status': status,
                'started': datetime.now().isoformat()
            },
            timeout=300  # 5 minutes, refresh on heartbeat
        )
    
    @staticmethod
    def get_process_info(stream_id):
        """Retrieve cached process info"""
        return cache.get(StreamCache.get_stream_key(stream_id)) or {}
    
    @staticmethod
    def delete_process_info(stream_id):
        """Delete process info from cache"""
        cache.delete(StreamCache.get_stream_key(stream_id))


def get_temp_dir_for_stream(stream_id):
    """Get unique temp directory per stream (prevents conflicts)"""
    stream_dir = os.path.join(TEMP_DIR, str(stream_id))
    Path(stream_dir).mkdir(parents=True, exist_ok=True)
    return stream_dir


def download_s3_file_chunked(media_file, stream_id):
    """Download S3 file with progress tracking"""
    url = media_file.file.url
    stream_dir = get_temp_dir_for_stream(stream_id)
    temp_path = os.path.join(stream_dir, f"media_{media_file.id}.mp4")
    
    try:
        resp = requests.get(url, stream=True, timeout=FFMPEG_TIMEOUT)
        resp.raise_for_status()
        
        total_size = int(resp.headers.get('content-length', 0))
        downloaded = 0
        
        with open(temp_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    
                    if total_size:
                        progress = (downloaded / total_size) * 100
                        logger.debug(f"Downloaded {media_file.title}: {progress:.1f}%")
        
        logger.info(f"✅ Downloaded {media_file.title} ({total_size / (1024**2):.1f}MB)")
        return temp_path
        
    except Exception as e:
        logger.error(f"Failed to download {media_file.title}: {e}")
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def download_files_parallel(media_files, stream_id):
    """Download multiple files concurrently using ThreadPoolExecutor"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    file_paths = {}
    
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS) as executor:
        futures = {
            executor.submit(download_s3_file_chunked, mf, stream_id): mf 
            for mf in media_files
        }
        
        for future in as_completed(futures):
            media_file = futures[future]
            try:
                file_path = future.result()
                file_paths[media_file.id] = file_path
            except Exception as e:
                logger.error(f"Download failed for {media_file.title}: {e}")
                raise
    
    return file_paths

def fetch_playlist_videos(playlist_id, youtube_service):
    """Fetch video list from YouTube playlist"""
    videos = []
    next_page_token = None
    
    while True:
        response = youtube_service.playlistItems().list(
            part='snippet,contentDetails',
            playlistId=playlist_id,
            maxResults=50,
            pageToken=next_page_token
        ).execute()
        
        for item in response['items']:
            video_id = item['contentDetails']['videoId']
            videos.append({
                'video_id': video_id,
                'title': item['snippet']['title'],
                'url': f"https://www.youtube.com/watch?v={video_id}"
            })
        
        next_page_token = response.get('nextPageToken')
        if not next_page_token:
            break
    
    return videos

def create_youtube_playlist_file(stream):
    """Create FFmpeg playlist from Stream.playlist_videos by extracting actual video URLs"""
    import subprocess
    import json
    
    stream_dir = get_temp_dir_for_stream(stream.id)
    concat_path = os.path.join(stream_dir, 'youtube_playlist.txt')
    
    logger.info(f"Creating playlist file for stream {stream.id}")
    logger.info(f"Playlist videos: {stream.playlist_videos}")
    
    # Get playlist ID from your JSONField
    playlist_data = stream.playlist_videos
    if not playlist_data:
        raise Exception("No playlist data - stream.playlist_videos is empty")
    
    # Handle both list and dict formats
    if isinstance(playlist_data, list):
        if not playlist_data[0].get('youtube_playlist_id'):
            raise Exception(f"Invalid playlist data structure: {playlist_data}")
        playlist_id = playlist_data[0]['youtube_playlist_id']
    else:
        raise Exception(f"Expected list but got {type(playlist_data).__name__}: {playlist_data}")
    
    logger.info(f"Fetching videos for playlist {playlist_id}")
    
    # Authenticate YouTube
    yt_account = stream.youtube_account
    if not yt_account:
        raise Exception("No YouTube account associated with stream")
        
    credentials = Credentials(
        token=yt_account.access_token,
        refresh_token=yt_account.refresh_token,
        token_uri='https://oauth2.googleapis.com/token',
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET
    )
    
    youtube = build('youtube', 'v3', credentials=credentials)
    videos = fetch_playlist_videos(playlist_id, youtube)
    
    if not videos:
        raise Exception("No videos found in playlist")
    
    logger.info(f"Fetched {len(videos)} videos from playlist")
    
    # Extract direct video URLs using yt-dlp
    logger.info("Extracting direct video URLs with yt-dlp...")
    video_urls = []
    
    for i, video in enumerate(videos[:50]):  # Limit to 50 videos
        video_id = video['video_id']
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        
        try:
            logger.info(f"Extracting URL for video {i+1}: {video['title']}")
            
            # Use yt-dlp to get direct streaming URL
            cmd = [
                'yt-dlp',
                '-f', 'best[ext=mp4]',  # Get best quality MP4
                '-g',  # Get URL only
                '--no-warnings',
                video_url
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                direct_url = result.stdout.strip().split('\n')[0]  # Get first/best URL
                if direct_url:
                    video_urls.append({
                        'video_id': video_id,
                        'title': video['title'],
                        'url': direct_url
                    })
                    logger.info(f"✅ Extracted URL: {direct_url[:80]}...")
                else:
                    logger.warning(f"No URL returned for {video['title']}")
            else:
                logger.error(f"yt-dlp failed for {video['title']}: {result.stderr}")
                # Fallback: try without format specification
                try:
                    cmd_fallback = ['yt-dlp', '-g', '--no-warnings', video_url]
                    result = subprocess.run(cmd_fallback, capture_output=True, text=True, timeout=30)
                    if result.returncode == 0:
                        direct_url = result.stdout.strip().split('\n')[0]
                        if direct_url:
                            video_urls.append({
                                'video_id': video_id,
                                'title': video['title'],
                                'url': direct_url
                            })
                            logger.info(f"✅ Fallback extracted URL: {direct_url[:80]}...")
                except Exception as e:
                    logger.error(f"Fallback failed: {e}")
                    
        except subprocess.TimeoutExpired:
            logger.error(f"Timeout extracting URL for {video['title']}")
        except Exception as e:
            logger.error(f"Error extracting URL for {video['title']}: {e}")
    
    # If yt-dlp extraction failed, use YouTube URLs as fallback
    if not video_urls:
        logger.warning("⚠️ Direct URL extraction failed, using YouTube watch URLs as fallback")
        video_urls = [{
            'video_id': video['video_id'],
            'title': video['title'],
            'url': f"https://www.youtube.com/watch?v={video['video_id']}"
        } for video in videos[:50]]
    
    logger.info(f"Successfully prepared {len(video_urls)} video URLs")
    
    # Store videos in stream for frontend
    stream.playlist_videos = [{
        'youtube_playlist_id': playlist_id,
        'videos': video_urls,
        'total_videos': len(video_urls),
        'extracted': True
    }]
    stream.save()
    logger.info(f"Updated stream with {len(video_urls)} extracted videos")
    
    # Create FFmpeg concat file with DIRECT VIDEO URLs
    with open(concat_path, 'w') as f:
        f.write("ffconcat version 1.0\n")
        for i, video in enumerate(video_urls):
            # Escape quotes and special characters in URL
            escaped_url = video['url'].replace("'", "'\\''")
            f.write(f"file '{escaped_url}'\n")
            logger.debug(f"Added video {i+1}: {video['title']}")
    
    logger.info(f"Playlist file created: {concat_path} with {len(video_urls)} videos")
    return concat_path



def create_concat_file(media_files, file_paths, stream_id, loops=50):
    """Create FFmpeg concat demuxer file with proper escaping"""
    stream_dir = get_temp_dir_for_stream(stream_id)
    concat_path = os.path.join(stream_dir, 'concat.txt')
    
    with open(concat_path, 'w') as f:
        for loop in range(loops):
            for media_file in media_files:
                file_path = file_paths[media_file.id]
                # Escape special characters for FFmpeg
                escaped = file_path.replace("\\", "\\\\").replace("'", "\\'")
                f.write(f"file '{escaped}'\n")
    
    return concat_path


def resolve_ffmpeg_binary():
    """Resolve FFmpeg path with fallbacks"""
    paths = [
        os.getenv('FFMPEG_PATH'),
        '/usr/bin/ffmpeg',
        '/usr/local/bin/ffmpeg',
        'ffmpeg'
    ]
    
    for path in paths:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            logger.info(f"Using FFmpeg: {path}")
            return path
    
    raise RuntimeError("FFmpeg not found. Install: apt install ffmpeg")


# ============ YOUTUBE PLAYLIST DOWNLOADER ============

def download_youtube_playlist_videos(stream, max_videos=50):
    """
    Download all videos from YouTube playlist and store as MediaFile objects
    
    Args:
        stream: Stream object with playlist_videos
        max_videos: Maximum videos to download (default 50)
    
    Returns:
        List of created MediaFile objects
    """
    from apps.streaming.models import MediaFile
    import mimetypes
    
    MediaFile = apps.get_model('streaming', 'MediaFile')
    
    # Get playlist data
    playlist_data = stream.playlist_videos
    if not playlist_data or not isinstance(playlist_data, list):
        raise Exception("Invalid playlist_videos structure")
    
    playlist_info = playlist_data[0]
    videos = playlist_info.get('videos', [])[:max_videos]
    
    if not videos:
        raise Exception(f"No videos in playlist")
    
    logger.info(f"📥 Starting download of {len(videos)} videos from playlist")
    
    created_media_files = []
    downloads_dir = get_temp_dir_for_stream(stream.id)
    
    for idx, video in enumerate(videos, 1):
        try:
            video_id = video['video_id']
            video_title = video['title']
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            
            logger.info(f"[{idx}/{len(videos)}] Downloading: {video_title}")
            
            # Step 1: Extract direct video URL using yt-dlp
            logger.debug(f"  Extracting stream URL with yt-dlp...")
            cmd = [
                'yt-dlp',
                '-f', 'best[ext=mp4]',  # Best MP4 format
                '-o', os.path.join(downloads_dir, f'video_{video_id}.mp4'),
                '--quiet',
                '--no-warnings',
                '--progress',
                video_url
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600  # 10 minutes per video
            )
            
            if result.returncode != 0:
                logger.error(f"  ✗ Download failed: {result.stderr[:200]}")
                continue
            
            # Step 2: Verify file was created
            file_path = os.path.join(downloads_dir, f'video_{video_id}.mp4')
            if not os.path.exists(file_path):
                logger.error(f"  ✗ File not created at {file_path}")
                continue
            
            file_size = os.path.getsize(file_path)
            logger.info(f"  ✓ Downloaded: {file_size / (1024**2):.1f} MB")
            
            # Step 3: Get video duration using FFprobe
            duration = get_video_duration(file_path)
            logger.debug(f"  Duration: {duration:.1f} seconds")
            
            # Step 4: Create MediaFile object
            logger.debug(f"  Creating MediaFile object...")
            
            with open(file_path, 'rb') as f:
                from django.core.files.base import ContentFile
                
                media_file = MediaFile.objects.create(
                    user=stream.user,
                    title=video_title,
                    media_type='video',
                    mime_type='video/mp4',
                    duration=duration,
                    file_size=file_size,
                    sequence=idx - 1
                )
                
                # Upload file to storage
                media_file.file.save(
                    f'youtube_{video_id}.mp4',
                    ContentFile(f.read()),
                    save=True
                )
            
            # Step 5: Attach to stream
            stream.media_files.add(media_file)
            created_media_files.append(media_file)
            
            logger.info(f"  ✅ Created MediaFile: {media_file.id}")
            
            # Clean up temporary file
            try:
                os.remove(file_path)
            except:
                pass
            
        except subprocess.TimeoutExpired:
            logger.error(f"  ✗ Timeout downloading: {video_title}")
            continue
        except Exception as e:
            logger.error(f"  ✗ Error: {e}")
            continue
    
    logger.info(f"✅ Download complete: {len(created_media_files)}/{len(videos)} videos")
    
    # Update stream status
    stream.playlist_videos = [{
        'youtube_playlist_id': playlist_info.get('youtube_playlist_id'),
        'original_videos': videos,
        'downloaded_videos': len(created_media_files),
        'media_file_ids': [mf.id for mf in created_media_files],
        'status': 'downloaded'
    }]
    stream.save()
    
    return created_media_files


def get_video_duration(file_path):
    """Get video duration using FFprobe"""
    try:
        cmd = [
            'ffprobe',
            '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1:noprint_names=1',
            file_path
        ]
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode == 0:
            duration = float(result.stdout.strip())
            return duration
    except Exception as e:
        logger.warning(f"Could not get duration: {e}")
    
    return 0.0


# ============ STREAM MANAGER ============

class StreamManager:
    """Production-grade streaming manager with graceful shutdown"""
    
    def __init__(self, stream):
        self.stream = stream
        self.youtube = None
        self.temp_dir = get_temp_dir_for_stream(stream.id)
        self.monitor_thread = None
        self.ffmpeg_process = None
    
    def authenticate_youtube(self) -> bool:
        """Authenticate with YouTube API + token refresh"""
        try:
            yt_account = self.stream.youtube_account
            credentials = Credentials(
                token=yt_account.access_token,
                refresh_token=yt_account.refresh_token,
                token_uri='https://oauth2.googleapis.com/token',
                client_id=settings.GOOGLE_CLIENT_ID,
                client_secret=settings.GOOGLE_CLIENT_SECRET
            )
            
            # CRITICAL: Refresh token if expired
            if credentials.expired and credentials.refresh_token:
                try:
                    request = Request()
                    credentials.refresh(request)
                    # Save refreshed token
                    yt_account.access_token = credentials.token
                    yt_account.token_expiry = credentials.expiry
                    yt_account.save()
                    logger.info(f"✅ Token refreshed, valid until {credentials.expiry}")
                except Exception as e:
                    logger.error(f"Token refresh failed: {e}")
                    return False
            
            self.youtube = build('youtube', 'v3', credentials=credentials)
            logger.info(f"✅ YouTube authenticated for {self.stream.id}")
            return True
        except Exception as e:
            logger.error(f"YouTube auth failed: {e}")
            return False
    
    def download_playlist_videos(self, max_videos=50) -> int:
        """
        Download all videos from YouTube playlist and store as MediaFile objects
        
        Returns:
            Number of videos downloaded
        """
        try:
            logger.info(f"🎬 Downloading playlist videos for stream {self.stream.id}...")
            
            if not self.stream.playlist_videos:
                raise Exception("Stream has no playlist data")
            
            media_files = download_youtube_playlist_videos(self.stream, max_videos=max_videos)
            
            logger.info(f"✅ Successfully downloaded {len(media_files)} videos")
            return len(media_files)
            
        except Exception as e:
            logger.error(f"Playlist download failed: {e}", exc_info=True)
            raise
    
    def create_broadcast(self) -> Optional[str]:
        """Create YouTube live broadcast with thumbnail"""
        logger.info(f"🔍 Creating broadcast for stream {self.stream.id}")
        logger.info(f"🔍 Stream URL before: {self.stream.stream_url}")
        try:
            if not self.youtube and not self.authenticate_youtube():
                return None
            
            # Create broadcast
            broadcast_body = {
                'snippet': {
                    'title': self.stream.title,
                    'description': self.stream.description,
                    'scheduledStartTime': (datetime.utcnow() + timedelta(seconds=30)).isoformat() + 'Z'
                },
                'status': {
                    'privacyStatus': 'public',
                    'selfDeclaredMadeForKids': False
                },
                'contentDetails': {
                    'enableAutoStart': True,   # ✅ Auto-start when stream ready
                    'enableAutoStop': True,    # Auto-stop after encoder stops
                    'enableDvr': True,
                    'recordFromStart': True,
                }
            }
            
            broadcast = self._execute_with_timeout(
                self.youtube.liveBroadcasts().insert(
                    part='snippet,status,contentDetails',
                    body=broadcast_body
                ),
                timeout=30
            )
            
            broadcast_id = broadcast['id']
            logger.info(f"✅ Broadcast created: {broadcast_id}")
            
            # Upload thumbnail
            if self.stream.thumbnail:
                self._upload_thumbnail(broadcast_id)
            
            # Create stream
            stream_resp = self._execute_with_timeout(
                self.youtube.liveStreams().insert(
                    part='snippet,cdn,status',
                    body={
                        'snippet': {'title': f"{self.stream.title} - Stream"},
                        'cdn': {
                            'frameRate': 'variable',
                            'ingestionType': 'rtmp',
                            'resolution': 'variable'
                        }
                    }
                ),
                timeout=30
            )
            
            stream_id = stream_resp['id']
            stream_key = stream_resp['cdn']['ingestionInfo']['streamName']
            ingestion_addr = stream_resp['cdn']['ingestionInfo']['ingestionAddress']
            
            # Bind broadcast to stream
            self._execute_with_timeout(
                self.youtube.liveBroadcasts().bind(
                    part='id,contentDetails',
                    id=broadcast_id,
                    streamId=stream_id
                ),
                timeout=30
            )
            
            # Save to DB
            self.stream.broadcast_id = broadcast_id
            self.stream.stream_key = stream_key
            self.stream.stream_url = f"{ingestion_addr}/{stream_key}"
            self.stream.save()
            
            return broadcast_id
            
        except Exception as e:
            logger.error(f"Broadcast creation failed: {e}", exc_info=True)
            self._set_error(str(e))
            return None
    
    def _execute_with_timeout(self, request, timeout=30):
        """Execute YouTube API request with timeout"""
        try:
            request.http.timeout = timeout
            return request.execute()
        except HttpError as e:
            logger.error(f"YouTube API error: {e.resp.status} - {e.content}")
            raise
        except Exception as e:
            logger.error(f"API request timeout or error: {e}")
            raise
    
    def _upload_thumbnail(self, broadcast_id):
        """Upload thumbnail to YouTube with retry - supports both local and S3 files"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Detect correct mimetype from file extension
                thumb_name = self.stream.thumbnail.name.lower()
                if thumb_name.endswith('.png'):
                    mimetype = 'image/png'
                else:
                    mimetype = 'image/jpeg'
                
                # Try to get thumbnail data from local file first (more reliable)
                thumb_data = None
                try:
                    with self.stream.thumbnail.open('rb') as f:
                        thumb_data = f.read()
                    logger.info("📁 Thumbnail loaded from local storage")
                except Exception as local_err:
                    logger.warning(f"Local thumbnail access failed, trying HTTP: {local_err}")
                    # Fallback to HTTP/S3 URL
                    thumb_url = self.stream.thumbnail.url
                    if not thumb_url.startswith('http'):
                        thumb_url = f"https://{settings.AWS_S3_CUSTOM_DOMAIN}{thumb_url}"
                    resp = requests.get(thumb_url, timeout=30)
                    resp.raise_for_status()
                    thumb_data = resp.content
                    logger.info("🌐 Thumbnail loaded from HTTP/S3")
                
                media = MediaIoBaseUpload(
                    io.BytesIO(thumb_data),
                    mimetype=mimetype,
                    resumable=True
                )
                
                self._execute_with_timeout(
                    self.youtube.thumbnails().set(
                        videoId=broadcast_id,
                        media_body=media
                    ),
                    timeout=30
                )
                
                logger.info(f"✅ Thumbnail uploaded to YouTube ({mimetype})")
                return True
                
            except Exception as e:
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    logger.warning(f"Thumbnail upload attempt {attempt+1} failed, retrying in {wait}s... Error: {e}")
                    time.sleep(wait)
                else:
                    logger.error(f"Thumbnail upload failed after {max_retries} attempts: {e}", exc_info=True)
                    return False
        
        return False
    
    def start_ffmpeg_stream(self):
        """Start FFmpeg streaming - handles both local media and YouTube playlists"""

        try:
            # DETECT STREAM TYPE
            has_local_media = self.stream.media_files.exists()
            has_youtube_playlist = bool(self.stream.playlist_videos)
            
            logger.info(f"📊 Stream type detection - Local media: {has_local_media}, YouTube playlist: {has_youtube_playlist}")
            
            if has_local_media:
                # 🎥 PATH 1: STREAM LOCAL MEDIA FILES
                logger.info(f"🎬 Starting stream with LOCAL MEDIA files ({self.stream.media_files.count()} files)")
                return self._start_local_media_stream()
            
            elif has_youtube_playlist:
                # 📺 PATH 2: STREAM YOUTUBE PLAYLIST
                logger.info("📺 Starting stream with YOUTUBE PLAYLIST")
                return self._start_youtube_playlist_stream()
            
            else:
                raise Exception("Stream has no media files or YouTube playlist attached")
        
        except Exception as e:
            logger.error(f"FFmpeg start failed: {e}", exc_info=True)
            self._set_error(str(e))
            return None
    
    def _start_local_media_stream(self):
        """Start streaming with local media files"""
        try:
            media_files = list(self.stream.media_files.all())
            if not media_files:
                raise Exception("No media files found")
            
            logger.info(f"⬇️ Downloading {len(media_files)} media files...")
            file_paths = download_files_parallel(media_files, self.stream.id)
            logger.info(f"✅ Downloaded {len(file_paths)} files")
            
            # Create concat file with local paths
            concat_path = create_concat_file(media_files, file_paths, self.stream.id, loops=50)
            logger.info(f"✅ Concat file created: {concat_path}")
            
            # Verify concat file
            self._verify_concat_file(concat_path)
            
            # Build FFmpeg command for local media
            ffmpeg_cmd = self._build_youtube_ffmpeg_command(concat_path)
            logger.info(f"FFmpeg command built with {len(ffmpeg_cmd)} arguments")
            
            # Spawn FFmpeg process
            logger.info("🚀 Spawning FFmpeg process...")
            self.ffmpeg_process = self._spawn_ffmpeg(ffmpeg_cmd)
            
            if not self.ffmpeg_process:
                raise Exception("FFmpeg process failed to spawn")
            
            logger.info(f"✅ FFmpeg spawned with PID: {self.ffmpeg_process.pid}")
            
            # Update stream status
            self.stream.status = 'running'
            self.stream.process_id = self.ffmpeg_process.pid
            self.stream.started_at = datetime.now()
            self.stream.process_started_at = datetime.now()
            self.stream.error_message = ''
            self.stream.save()
            
            logger.info(f"✅ LOCAL MEDIA STREAM STARTED - PID: {self.ffmpeg_process.pid}, URL: {self.stream.stream_url[:60]}...")
            return self.ffmpeg_process.pid
        
        except Exception as e:
            logger.error(f"Local media stream failed: {e}", exc_info=True)
            raise
    
    def _start_youtube_playlist_stream(self):
        """Start streaming with YouTube playlist - DOWNLOADS VIDEOS FIRST"""
        try:
            # NEW: Check if videos are already downloaded to storage
            has_downloaded_media = self.stream.media_files.exists()
            
            if has_downloaded_media:
                # Videos already downloaded, stream from storage
                logger.info(f"📁 Videos already downloaded ({self.stream.media_files.count()} files)")
                logger.info("Switching to local media stream mode...")
                return self._start_local_media_stream()
            
            # NEW: Download all playlist videos to storage first
            logger.info("📥 Downloading playlist videos to storage...")
            
            try:
                media_files = download_youtube_playlist_videos(self.stream, max_videos=50)
                
                if not media_files:
                    raise Exception("Failed to download any videos from playlist")
                
                logger.info(f"✅ Downloaded {len(media_files)} videos to storage")
                
                # Now stream from the downloaded files
                logger.info("📁 Switching to local media stream mode...")
                return self._start_local_media_stream()
                
            except Exception as download_error:
                logger.error(f"Download failed: {download_error}")
                logger.warning("⚠️ Falling back to direct YouTube streaming (less reliable)...")
                
                # FALLBACK: Try direct YouTube URL streaming (original method)
                return self._start_youtube_url_stream()
        
        except Exception as e:
            logger.error(f"YouTube playlist stream failed: {e}", exc_info=True)
            raise
    
    def _start_youtube_url_stream(self):
        """Fallback: Stream directly from YouTube URLs (less reliable)"""
        try:
            logger.warning("⚠️ Using fallback YouTube URL streaming (not recommended)")
            
            # 1. Create video playlist file
            logger.info(f"Creating YouTube playlist file for stream {self.stream.id}...")
            concat_path = create_youtube_playlist_file(self.stream)
            logger.info(f"✅ Playlist file created: {concat_path}")
            
            # 2. Verify concat file contents
            self._verify_concat_file(concat_path)
        
            # 3. Build FFmpeg command
            logger.info("Building FFmpeg command...")
            ffmpeg_cmd = self._build_youtube_ffmpeg_command(concat_path)
            logger.info(f"FFmpeg command built with {len(ffmpeg_cmd)} arguments")
            for i, arg in enumerate(ffmpeg_cmd):
                if 'http' in str(arg).lower() or 'youtube' in str(arg).lower() or '.txt' in str(arg).lower():
                    logger.info(f"  Arg {i}: {str(arg)[:100]}")
        
            # 4. Spawn FFmpeg process
            logger.info("🚀 Spawning FFmpeg process...")
            self.ffmpeg_process = self._spawn_ffmpeg(ffmpeg_cmd)
            
            if not self.ffmpeg_process:
                raise Exception("FFmpeg process failed to spawn")
            
            logger.info(f"FFmpeg spawned with PID: {self.ffmpeg_process.pid}")
        
            # 5. Update stream status
            self.stream.status = 'running'
            self.stream.process_id = self.ffmpeg_process.pid
            self.stream.started_at = datetime.now()
            self.stream.process_started_at = datetime.now()
            self.stream.error_message = ''
            self.stream.save()
        
            logger.info(f"✅ YOUTUBE URL STREAM STARTED - PID: {self.ffmpeg_process.pid}, URL: {self.stream.stream_url[:60]}...")
            return self.ffmpeg_process.pid
        
        except Exception as e:
            logger.error(f"YouTube URL stream failed: {e}", exc_info=True)
            raise

    
    def _verify_concat_file(self, concat_path: str):
        """Verify concat file has valid URLs"""
        try:
            with open(concat_path, 'r') as f:
                content = f.read()
            
            logger.info(f"Concat file size: {len(content)} bytes")
            logger.info(f"Concat file first 200 chars:\\n{content[:200]}")
            
            # Count URLs
            url_count = content.count('http')
            logger.info(f"Found {url_count} HTTP URLs in concat file")
            
            if url_count == 0:
                logger.warning("⚠️ No HTTP URLs found in concat file!")
            else:
                logger.info(f"✅ Concat file has {url_count} video URLs ready")
                
        except Exception as e:
            logger.error(f"Failed to verify concat file: {e}")
        
    '''
    def start_ffmpeg_stream(self):
        """Start FFmpeg streaming - MAIN METHOD"""
        try:
            media_files = list(self.stream.media_files.all())
            if not media_files:
                raise Exception("No media files attached")
            
            logger.info(f"🚀 Starting stream {self.stream.id} with {len(media_files)} files")
            
            # Step 1: Download all files in parallel
            logger.info("⬇️ Downloading media files...")
            file_paths = download_files_parallel(media_files, self.stream.id)
            logger.info(f"✅ All {len(file_paths)} files downloaded")
            
            # Step 2: Create concat file
            concat_path = create_concat_file(media_files, file_paths, self.stream.id, loops=100)
            logger.info(f"✅ Concat file created: {concat_path}")
            
            # Step 3: Build FFmpeg command
            ffmpeg_cmd = self._build_ffmpeg_command(concat_path)
            
            # Step 4: Start FFmpeg
            self.ffmpeg_process = self._spawn_ffmpeg(ffmpeg_cmd)
            
            # Step 5: Start monitoring thread
            self._start_monitor_thread(ffmpeg_cmd)
            
            # Step 6: Update database
            self.stream.process_id = self.ffmpeg_process.pid
            self.stream.process_started_at = datetime.now()
            self.stream.status = 'running'
            self.stream.started_at = datetime.now()
            self.stream.save()
            
            # Cache process info
            StreamCache.set_process_info(self.stream.id, self.ffmpeg_process.pid, 'running')
            
            logger.info(f"✅ Stream LIVE! PID: {self.ffmpeg_process.pid}")
            return self.ffmpeg_process.pid
            
        except Exception as e:
            logger.error(f"❌ Stream start failed: {e}", exc_info=True)
            self._set_error(str(e))
            self._cleanup_temp_files()
            return None
    '''
    

    def _build_youtube_ffmpeg_command(self, concat_path: str) -> list:
        """Build FFmpeg command for YouTube streaming with proper network handling"""
        logger.info(f"🔍 FFmpeg using concat: {concat_path}")
        logger.info(f"🔍 File exists: {os.path.exists(concat_path)}")
        
        ffmpeg_bin = resolve_ffmpeg_binary()
    
        return [
            ffmpeg_bin,
            # Input reading with native frame rate
            '-re',
            
            # Input options for better URL handling
            '-connection_timeout', '5000000',  # 5 seconds timeout for connection
            '-socket_timeout', '5000000',      # 5 seconds timeout for socket
            '-http_persistent', '0',           # Don't persist HTTP connections
            '-reconnect', '1',                 # Reconnect on network failure
            '-reconnect_streamed', '1',        # Reconnect for streamed content
            '-reconnect_delay_max', '5',       # Max 5s delay between reconnections
            '-rtbufsize', '50M',               # Larger buffer for streaming
            
            # Logging
            '-loglevel', 'info',
            
            # Input concat file
            '-f', 'concat',
            '-safe', '0',
            '-i', concat_path,
            
            # Video encoding
            '-c:v', 'libx264',
            '-preset', 'fast',               # Fast encoding for real-time streaming
            '-profile:v', 'main',
            '-level', '4.1',
            '-b:v', '2500k',                 # 2.5 Mbps video bitrate
            '-maxrate', '3500k',
            '-bufsize', '7000k',
            '-g', '60',                      # Keyframe every 2 seconds (60 frames at 30fps)
            '-keyint_min', '60',
            '-pix_fmt', 'yuv420p',
            '-thread_queue_size', '512',    # Larger queue for network streams
            
            # Audio encoding
            '-c:a', 'aac',
            '-b:a', '128k',
            '-ar', '44100',
            '-ac', '2',
            
            # Output to YouTube RTMP with FLV format
            '-f', 'flv',
            '-flvflags', 'no_duration_filesize',
            
            # Output URL
            self.stream.stream_url
        ]
    
    
    '''
    def _build_ffmpeg_command(self, concat_path: str) -> list:
        """Build production-grade FFmpeg command"""
        ffmpeg_bin = resolve_ffmpeg_binary()
        
        return [
            ffmpeg_bin,
            
            # Input
            '-re',
            '-f', 'concat',
            '-safe', '0',
            '-i', concat_path,
            
            # Video encoding
            '-c:v', 'libx264',
            '-preset', 'veryfast',
            '-profile:v', 'main',
            '-level', '4.1',
            '-b:v', '3000k',
            '-maxrate', '4000k',
            '-bufsize', '8000k',
            '-g', '60',
            '-keyint_min', '60',
            '-pix_fmt', 'yuv420p',
            '-movflags', 'frag_keyframe+empty_moov',
            
            # Audio
            '-c:a', 'aac',
            '-b:a', '128k',
            '-ar', '44100',
            '-ac', '2',
            
            # Output - FLV for RTMP
            '-f', 'flv',
            '-flvflags', 'no_duration_filesize',
            
            # Network settings
            '-rtbufsize', STREAM_BUFFER_SIZE,
            '-fflags', 'nobuffer',
            '-flags', 'low_delay',
            
            # Output URL
            self.stream.stream_url
        ]
    '''
    
    def _spawn_ffmpeg(self, cmd: list) -> subprocess.Popen:
        """Spawn FFmpeg process"""
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,  # CRITICAL: Allow stdin for graceful shutdown
                preexec_fn=os.setsid,
                universal_newlines=True,
                bufsize=1
            )
            
            # Log FFmpeg output asynchronously
            threading.Thread(
                target=self._log_ffmpeg_output,
                args=(process.stderr,),
                daemon=True
            ).start()
            
            logger.info(f"FFmpeg spawned: PID {process.pid}")
            return process
            
        except Exception as e:
            logger.error(f"Failed to spawn FFmpeg: {e}")
            raise
    
    def _log_ffmpeg_output(self, stderr):
        """Log FFmpeg stderr in real-time"""
        try:
            for line in iter(stderr.readline, ''):
                if line:
                    logger.info(f"FFmpeg: {line.strip()}")
        except:
            pass
    
    def _start_monitor_thread(self, cmd: list):
        """Start monitoring thread for auto-restart"""
        self.monitor_thread = threading.Thread(
            target=self._monitor_ffmpeg,
            args=(cmd,),
            daemon=True
        )
        self.monitor_thread.start()
    
    def _monitor_ffmpeg(self, cmd: list):
        """Monitor FFmpeg and auto-restart on failure"""
        restarts = 0
        current_proc = self.ffmpeg_process
        start_time = time.time()
        max_uptime_seconds = 168 * 3600  # 1 week
        
        while restarts < MAX_STREAM_RESTARTS:
            # Heartbeat: refresh cache every minute
            StreamCache.set_process_info(self.stream.id, current_proc.pid, 'running')
            self.stream.last_heartbeat = datetime.now()
            self.stream.save()
            
            try:
                ret = current_proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                continue  # Continue monitoring
            
            logger.warning(f"FFmpeg exited (code={ret}), restart #{restarts}")
            
            # Graceful restart after uptime threshold
            if time.time() - start_time > max_uptime_seconds:
                logger.warning("Stream reached max uptime, restarting...")
                start_time = time.time()
                restarts = 0
            
            if ret == 0 or ret == 143:  # Normal exit or SIGTERM
                break
            
            restarts += 1
            backoff = min(60, 5 * restarts)
            logger.info(f"Restarting in {backoff}s...")
            time.sleep(backoff)
            
            try:
                current_proc = self._spawn_ffmpeg(cmd)
                self.stream.process_id = current_proc.pid
                self.stream.process_started_at = datetime.now()
                self.stream.status = 'running'
                self.stream.save()
                
                StreamCache.set_process_info(self.stream.id, current_proc.pid, 'running')
                logger.info(f"Restarted: New PID {current_proc.pid}")
                
            except Exception as e:
                logger.error(f"Restart failed: {e}")
                break
        
        # Final cleanup
        self._finalize_stream(restarts)
    
    def _finalize_stream(self, restarts: int):
        """Clean up after stream ends"""
        try:
            self.stream.process_id = None
            self.stream.process_started_at = None
            self.stream.last_heartbeat = None
            self.stream.status = 'error' if restarts >= MAX_STREAM_RESTARTS else 'stopped'
            self.stream.error_message = f'FFmpeg failed after {restarts} restarts'
            self.stream.stopped_at = datetime.now()
            self.stream.save()
            
            StreamCache.delete_process_info(self.stream.id)
            self._cleanup_temp_files()
            
            logger.info(f"Stream {self.stream.id} finalized")
        except Exception as e:
            logger.error(f"Finalization failed: {e}")
    
    def _cleanup_temp_files(self):
        """Remove temporary files"""
        try:
            import shutil
            if os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir)
                logger.info(f"Cleaned up: {self.temp_dir}")
        except Exception as e:
            logger.warning(f"Cleanup failed: {e}")
    
    def _set_error(self, error_msg: str):
        """Set stream error state"""
        self.stream.status = 'error'
        self.stream.error_message = error_msg
        self.stream.save()
    
    def stop_stream(self) -> bool:
        """🎬 GRACEFUL SHUTDOWN - Stop stream properly"""
        try:
            logger.info(f"⏹️ Stopping stream {self.stream.id} gracefully...")
            
            # STEP 1: End YouTube broadcast FIRST (prevents buffering)
            if self.stream.broadcast_id:
                success = self._end_youtube_broadcast()
                if not success:
                    logger.error("Failed to end YouTube broadcast, force stopping anyway")
            
            # STEP 2: Gracefully stop FFmpeg (write 'q' to stdin)
            if self.ffmpeg_process and self.ffmpeg_process.poll() is None:
                self._graceful_ffmpeg_stop()
            
            # STEP 3: Wait for process to exit
            if self.ffmpeg_process:
                try:
                    self.ffmpeg_process.wait(timeout=GRACEFUL_SHUTDOWN_TIMEOUT)
                    logger.info("FFmpeg exited gracefully")
                except subprocess.TimeoutExpired:
                    logger.warning("FFmpeg didn't exit gracefully, force killing...")
                    os.killpg(os.getpgid(self.ffmpeg_process.pid), signal.SIGKILL)
            
            # STEP 4: Update database
            self.stream.status = 'stopped'
            self.stream.stopped_at = datetime.now()
            self.stream.process_id = None
            self.stream.process_started_at = None
            self.stream.last_heartbeat = None
            self.stream.save()
            
            # Cleanup
            StreamCache.delete_process_info(self.stream.id)
            self._cleanup_temp_files()
            
            logger.info(f"✅ Stream {self.stream.id} stopped successfully")
            return True
            
        except Exception as e:
            logger.error(f"❌ Stop failed: {e}", exc_info=True)
            return False
    
    def _end_youtube_broadcast(self) -> bool:
        """🎥 END YOUTUBE BROADCAST - This prevents buffering"""
        try:
            if not self.youtube and not self.authenticate_youtube():
                return False
            
            logger.info(f"Ending YouTube broadcast {self.stream.broadcast_id}...")
            
            # Transition broadcast to complete
            self._execute_with_timeout(
                self.youtube.liveBroadcasts().transition(
                    broadcastStatus='complete',
                    id=self.stream.broadcast_id,
                    part='status'
                ),
                timeout=BROADCAST_END_TIMEOUT
            )
            
            logger.info("✅ YouTube broadcast ended")
            return True
            
        except HttpError as e:
            if e.resp.status == 403:
                logger.warning("Broadcast already ended or permission denied")
                return True  # Not critical
            else:
                logger.error(f"YouTube API error: {e}")
                return False
        except Exception as e:
            logger.error(f"Failed to end broadcast: {e}")
            return False
    
    def _graceful_ffmpeg_stop(self):
        """💤 Gracefully stop FFmpeg by sending 'q' to stdin"""
        try:
            if self.ffmpeg_process and self.ffmpeg_process.stdin:
                logger.info("Sending graceful stop command to FFmpeg...")
                self.ffmpeg_process.stdin.write('q\n')
                self.ffmpeg_process.stdin.flush()
                logger.info("Graceful stop command sent")
            else:
                logger.warning("Cannot send graceful stop, stdin not available")
        except Exception as e:
            logger.warning(f"Graceful stop failed: {e}")


# ============ CELERY TASKS ============

@shared_task(
    time_limit=CELERY_TASK_TIMEOUT,
    soft_time_limit=CELERY_TASK_TIMEOUT - 300,
    acks_late=True,
    reject_on_worker_lost=True
)
def start_stream_task(stream_id: int):
    """Celery task to start stream"""
    try:
        Stream = apps.get_model('streaming', 'Stream')
        stream = Stream.objects.get(pk=stream_id)
        
        manager = StreamManager(stream)
        return manager.start_ffmpeg_stream()
        
    except Stream.DoesNotExist:
        logger.error(f"Stream {stream_id} not found")
        raise
    except Exception as e:
        logger.error(f"Stream task failed: {e}", exc_info=True)
        raise


@shared_task
def stop_stream_task(stream_id: int):
    """Celery task to stop stream"""
    try:
        Stream = apps.get_model('streaming', 'Stream')
        stream = Stream.objects.get(pk=stream_id)
        
        manager = StreamManager(stream)
        return manager.stop_stream()
        
    except Exception as e:
        logger.error(f"Stop task failed: {e}")
        return False


@shared_task
def cleanup_orphaned_broadcasts():
    """Cleanup broadcasts that are stuck in live state"""
    try:
        Stream = apps.get_model('streaming', 'Stream')
        YouTubeAccount = apps.get_model('accounts', 'YouTubeAccount')
        
        # Find streams that crashed but didn't end YouTube broadcast
        stuck_streams = Stream.objects.filter(
            status__in=['error', 'stopped'],
            broadcast_id__isnull=False
        ).exclude(broadcast_id='')
        
        for stream in stuck_streams:
            try:
                manager = StreamManager(stream)
                if not manager.authenticate_youtube():
                    continue
                
                # Try to end broadcast
                manager._end_youtube_broadcast()
                stream.broadcast_id = ''
                stream.save()
                logger.info(f"Cleaned up broadcast for stream {stream.id}")
            except Exception as e:
                logger.warning(f"Cleanup failed for stream {stream.id}: {e}")
    
    except Exception as e:
        logger.error(f"Cleanup task failed: {e}")
