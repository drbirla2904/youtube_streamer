"""
stream_manager.py — YouTube playlist streaming via yt-dlp → FFmpeg stdin pipe.

AUTHENTICATION ARCHITECTURE (permanent, multi-user):
─────────────────────────────────────────────────────
Two completely separate auth concerns:

1. USER YOUTUBE ACCOUNT (per-user, already working)
   • OAuth2 via google-api-python-client
   • Used for: create broadcast, liveStream, bind, transition to live
   • Stored in YouTubeAccount model per user
   • Each user needs their own

2. PLATFORM yt-dlp DOWNLOAD TOKEN (server-wide, one-time setup)
   • YouTube OAuth2 TV device flow via yt-dlp built-in OAuth2
   • Used for: downloading/streaming video bytes from YouTube
   • Stored in ~/.cache/yt-dlp/youtube-oauth2.json on the server
   • Set up ONCE by admin: python manage.py setup_ytdlp_auth
   • Auto-refreshes forever — never expires unless manually revoked
   • ALL user streams share this single server token automatically

KEY ARCHITECTURE DECISION — Per-video FFmpeg restart:
─────────────────────────────────────────────────────
Each video in the playlist gets its own fresh FFmpeg process.
This eliminates timestamp discontinuity crashes when video N ends
and video N+1 starts with timestamps reset to 0.
FFmpeg connects to the same RTMP URL each time — YouTube keeps
the broadcast alive across reconnects.
"""

import subprocess
import os
import signal
import shutil
import sys
import time
import logging
import requests
import threading
import io
from typing import Optional, List, Dict
from datetime import datetime, timedelta
from pathlib import Path
from django.conf import settings
from django.apps import apps
from django.core.cache import cache
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from googleapiclient.errors import HttpError
from celery import shared_task

logger = logging.getLogger(__name__)

# ============ CONFIGURATION ============
TEMP_DIR = getattr(settings, 'STREAM_TEMP_DIR', '/var/tmp/streams')
MAX_CONCURRENT_DOWNLOADS = getattr(settings, 'MAX_CONCURRENT_DOWNLOADS', 3)
CHUNK_SIZE = 512 * 1024
FFMPEG_TIMEOUT = 300
MAX_STREAM_RESTARTS = 5
BROADCAST_END_TIMEOUT = 30
GRACEFUL_SHUTDOWN_TIMEOUT = 15
CELERY_TASK_TIMEOUT = 86400
STREAM_ACTIVE_POLL_INTERVAL = 5
STREAM_ACTIVE_MAX_WAIT = 120

YTDLP_COOKIES_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    getattr(settings, "YTDLP_COOKIES_FILE", "yt-cookies.txt")
)

# Platform-level yt-dlp OAuth2 token directory.
try:
    from platformdirs import user_cache_dir as _ucd
    _ytdlp_cache = _ucd("yt-dlp")
except ImportError:
    _ytdlp_cache = os.path.expanduser("~/.cache/yt-dlp")

YTDLP_TOKEN_DIR = getattr(settings, "YTDLP_OAUTH_TOKEN_DIR", _ytdlp_cache)
YTDLP_TOKEN_FILE = os.path.join(YTDLP_TOKEN_DIR, 'youtube-oauth2.json')

YTDLP_MAX_RETRIES = 3
YTDLP_RETRY_BACKOFF = 30  # seconds between retries (shorter — only retry real failures)

Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)


# ============ BINARY RESOLUTION ============

def resolve_ffmpeg_binary() -> str:
    candidates = []
    for src in [os.getenv('FFMPEG_PATH', ''), getattr(settings, 'FFMPEG_PATH', '')]:
        src = src.strip()
        if src and src not in candidates:
            candidates.append(src)
    for p in ['/usr/bin/ffmpeg', '/usr/local/bin/ffmpeg', '/snap/bin/ffmpeg',
              '/opt/homebrew/bin/ffmpeg', r'C:\ffmpeg\ffmpeg.exe']:
        if p not in candidates:
            candidates.append(p)
    found = shutil.which('ffmpeg')
    if found and found not in candidates:
        candidates.append(found)
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            logger.info(f"Using FFmpeg: {path}")
            return path
    raise RuntimeError(
        "FFmpeg not found.\n"
        "  sudo apt install ffmpeg\n"
        "  OR add FFMPEG_PATH=/usr/bin/ffmpeg to your .env"
    )


def resolve_ytdlp_binary() -> Optional[str]:
    for path in [shutil.which('yt-dlp'), '/usr/local/bin/yt-dlp',
                 '/usr/bin/yt-dlp', os.path.expanduser('~/.local/bin/yt-dlp')]:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    try:
        r = subprocess.run(['/bin/bash', '-c', 'which yt-dlp'],
                           capture_output=True, text=True, timeout=5)
        p = r.stdout.strip()
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass
    try:
        r = subprocess.run(['python3', '-m', 'yt_dlp', '--version'],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return '__module__'
    except Exception:
        pass
    return None


def _ytdlp_base_cmd() -> List[str]:
    ytdlp = resolve_ytdlp_binary()
    if ytdlp is None:
        raise RuntimeError(
            "yt-dlp not installed.\n"
            "  pip install -U yt-dlp\n"
            "  Then: python manage.py setup_ytdlp_auth"
        )
    return ['python3', '-m', 'yt_dlp'] if ytdlp == '__module__' else [ytdlp]


# ============ PLATFORM-LEVEL yt-dlp AUTH ============

def get_ytdlp_auth_args() -> List[str]:
    """
    Priority order:
    1. OAuth2 token (permanent, auto-refreshes) ← preferred
    2. Cookies file (fallback, expires)
    3. No auth (will likely fail on VPS IPs)
    """
    args = []

    # ── 1. OAuth2 token (permanent) ──────────────────────────────────────────
    if os.path.exists(YTDLP_TOKEN_FILE):
        logger.debug(f"Using platform OAuth2 token: {YTDLP_TOKEN_FILE}")
        args += ['--username', 'oauth2', '--password', '']
        args += ['--extractor-args', 'youtube:player_client=tv_embedded']
        return args  # OAuth2 is enough, no cookies needed

    # ── 2. Cookies fallback ──────────────────────────────────────────────────
    cookies_file = os.path.join(settings.BASE_DIR, 'yt-cookies.txt')
    if os.path.exists(cookies_file) and os.path.getsize(cookies_file) > 100:
        logger.info(f"🎯 Using cookies file: {cookies_file}")
        args += ['--cookies', cookies_file]
        args += ['--extractor-args', 'youtube:player_client=tv_embedded']
        return args

    # ── 3. No auth ───────────────────────────────────────────────────────────
    logger.warning(
        "⚠️  No auth configured. Run: python manage.py setup_ytdlp_auth"
    )
    args += ['--extractor-args', 'youtube:player_client=tv_embedded']
    return args

def ytdlp_auth_is_configured() -> bool:
    if os.path.exists(YTDLP_TOKEN_FILE):
        return True
    cookies_file = os.path.join(settings.BASE_DIR, 'yt-cookies.txt')
    if os.path.exists(cookies_file) and os.path.getsize(cookies_file) > 100:
        return True
    # Warn but don't block — let it attempt and fail with clear logs
    logger.warning("⚠️  No yt-dlp auth configured.")
    return False


# ============ CACHE ============

class StreamCache:
    @staticmethod
    def _key(sid): return f"stream:{sid}"

    @staticmethod
    def set_process_info(sid, pid, status):
        cache.set(StreamCache._key(sid),
                  {'pid': pid, 'status': status, 'started': datetime.now().isoformat()},
                  timeout=300)

    @staticmethod
    def get_process_info(sid):
        return cache.get(StreamCache._key(sid)) or {}

    @staticmethod
    def delete_process_info(sid):
        cache.delete(StreamCache._key(sid))


def get_temp_dir_for_stream(stream_id):
    d = os.path.join(TEMP_DIR, str(stream_id))
    Path(d).mkdir(parents=True, exist_ok=True)
    return d


# ============ FILE HELPERS ============

def download_s3_file_chunked(media_file, stream_id):
    temp_path = os.path.join(get_temp_dir_for_stream(stream_id), f"media_{media_file.id}.mp4")
    try:
        resp = requests.get(media_file.file.url, stream=True, timeout=FFMPEG_TIMEOUT)
        resp.raise_for_status()
        with open(temp_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    f.write(chunk)
        return temp_path
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def download_files_parallel(media_files, stream_id):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    file_paths = {}
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS) as executor:
        futures = {executor.submit(download_s3_file_chunked, mf, stream_id): mf
                   for mf in media_files}
        for future in as_completed(futures):
            mf = futures[future]
            file_paths[mf.id] = future.result()
    return file_paths


def create_concat_file(media_files, file_paths, stream_id, loops=50):
    concat_path = os.path.join(get_temp_dir_for_stream(stream_id), 'concat.txt')
    with open(concat_path, 'w') as f:
        for _ in range(loops):
            for mf in media_files:
                escaped = file_paths[mf.id].replace("\\", "\\\\").replace("'", "\\'")
                f.write(f"file '{escaped}'\n")
    return concat_path


# ============ YOUTUBE API HELPERS ============

def fetch_playlist_videos(playlist_id: str, youtube_service) -> List[Dict]:
    videos, token = [], None
    while True:
        resp = youtube_service.playlistItems().list(
            part='snippet,contentDetails', playlistId=playlist_id,
            maxResults=50, pageToken=token
        ).execute()
        for item in resp.get('items', []):
            vid = item['contentDetails']['videoId']
            videos.append({
                'video_id': vid,
                'title': item['snippet']['title'],
                'url': f"https://www.youtube.com/watch?v={vid}"
            })
        token = resp.get('nextPageToken')
        if not token:
            break
    return videos


def _fetch_and_cache_playlist_videos(stream) -> List[Dict]:
    data = stream.playlist_videos
    if not data or not isinstance(data, list):
        raise Exception("stream.playlist_videos is empty or invalid")
    info = data[0]
    playlist_id = info.get('youtube_playlist_id', '').strip()
    if not playlist_id:
        raise Exception("No youtube_playlist_id in playlist_videos")
    if info.get('videos'):
        return info['videos']

    logger.info(f"Fetching playlist {playlist_id} from YouTube API...")
    yt = stream.youtube_account
    creds = Credentials(
        token=yt.access_token, refresh_token=yt.refresh_token,
        token_uri='https://oauth2.googleapis.com/token',
        client_id=settings.GOOGLE_CLIENT_ID, client_secret=settings.GOOGLE_CLIENT_SECRET
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        yt.access_token = creds.token
        yt.token_expiry = creds.expiry
        yt.save(update_fields=['access_token', 'token_expiry'])

    youtube = build('youtube', 'v3', credentials=creds)
    videos = fetch_playlist_videos(playlist_id, youtube)
    if not videos:
        raise Exception(f"Playlist {playlist_id} has no videos")

    stream.playlist_videos = [{
        'youtube_playlist_id': playlist_id,
        'title': info.get('title', f'Playlist {playlist_id}'),
        'videos': videos, 'total_videos': len(videos), 'videos_fetched': True,
    }]
    stream.save(update_fields=['playlist_videos'])
    logger.info(f"Fetched and cached {len(videos)} videos")
    return videos


# ============ STREAM MANAGER ============

class StreamManager:

    def __init__(self, stream):
        self.stream = stream
        self.youtube = None
        self.temp_dir = get_temp_dir_for_stream(stream.id)
        self.monitor_thread = None
        self.ffmpeg_process = None
        self._live_stream_id = None
        self._stop_event = threading.Event()
        self._transition_done = False  # track if broadcast already transitioned to live

    # ── YouTube auth ──────────────────────────────────────────────────────────

    def authenticate_youtube(self) -> bool:
        try:
            yt = self.stream.youtube_account
            creds = Credentials(
                token=yt.access_token, refresh_token=yt.refresh_token,
                token_uri='https://oauth2.googleapis.com/token',
                client_id=settings.GOOGLE_CLIENT_ID,
                client_secret=settings.GOOGLE_CLIENT_SECRET
            )
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                yt.access_token = creds.token
                yt.token_expiry = creds.expiry
                yt.save()
            self.youtube = build('youtube', 'v3', credentials=creds)
            return True
        except Exception as e:
            logger.error(f"YouTube auth failed: {e}")
            return False

    # ── Broadcast creation ────────────────────────────────────────────────────

    def create_broadcast(self) -> Optional[str]:
        logger.info(f"Creating broadcast for stream {self.stream.id}")
        try:
            if not self.youtube and not self.authenticate_youtube():
                return None

            broadcast = self._yt(self.youtube.liveBroadcasts().insert(
                part='snippet,status,contentDetails',
                body={
                    'snippet': {
                        'title': self.stream.title,
                        'description': self.stream.description,
                        'scheduledStartTime': datetime.utcnow().isoformat() + 'Z',
                    },
                    'status': {'privacyStatus': 'public', 'selfDeclaredMadeForKids': False},
                    'contentDetails': {
                        'enableAutoStart': True, 'enableAutoStop': True,
                        'enableDvr': True, 'recordFromStart': True,
                        'latencyPreference': 'normal',
                    }
                }
            ))
            broadcast_id = broadcast['id']

            if self.stream.thumbnail:
                self._upload_thumbnail(broadcast_id)

            stream_resp = self._yt(self.youtube.liveStreams().insert(
                part='snippet,cdn,status',
                body={
                    'snippet': {'title': f"{self.stream.title} - Encoder"},
                    'cdn': {'frameRate': 'variable', 'ingestionType': 'rtmp',
                            'resolution': 'variable'}
                }
            ))

            self._live_stream_id = stream_resp['id']
            stream_key = stream_resp['cdn']['ingestionInfo']['streamName']
            ingestion_addr = stream_resp['cdn']['ingestionInfo']['ingestionAddress']

            self._yt(self.youtube.liveBroadcasts().bind(
                part='id,contentDetails', id=broadcast_id,
                streamId=self._live_stream_id
            ))
            logger.info(f"Broadcast {broadcast_id} bound to stream {self._live_stream_id}")

            self.stream.broadcast_id = broadcast_id
            self.stream.stream_key = stream_key
            self.stream.stream_url = f"{ingestion_addr}/{stream_key}"
            self.stream.save()

            logger.info(f"Broadcast {broadcast_id} ready — auto-starts when FFmpeg sends data")
            return broadcast_id

        except Exception as e:
            logger.error(f"Broadcast creation failed: {e}", exc_info=True)
            self._set_error(str(e))
            return None

    def _yt(self, request, timeout=30):
        try:
            request.http.timeout = timeout
            return request.execute()
        except HttpError as e:
            logger.error(f"YouTube API {e.resp.status}: {e.content}")
            raise

    def _upload_thumbnail(self, broadcast_id):
        for attempt in range(3):
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
                
                self._yt(self.youtube.thumbnails().set(
                    videoId=broadcast_id,
                    media_body=MediaIoBaseUpload(
                        io.BytesIO(thumb_data), mimetype=mimetype, resumable=True)
                ))
                logger.info(f"✅ Thumbnail uploaded to YouTube ({mimetype})")
                return True
            except Exception as e:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                else:
                    logger.error(f"Thumbnail upload failed: {e}", exc_info=True)
        return False

    # ── Broadcast → live transition ───────────────────────────────────────────

    def _wait_for_stream_active_and_transition(self):
        """Poll liveStream status and transition broadcast to live once active."""
        if not self._live_stream_id:
            return
        if self._transition_done:
            return  # Already transitioned — don't call again across video restarts
        logger.info(f"Monitoring liveStream {self._live_stream_id} status...")
        if not self.youtube and not self.authenticate_youtube():
            return

        elapsed = 0
        check_interval = 10
        max_wait = 300

        while elapsed < max_wait:
            if self._stop_event.is_set():
                return
            try:
                resp = self._yt(self.youtube.liveStreams().list(
                    part='status', id=self._live_stream_id
                ))
                items = resp.get('items', [])
                if items:
                    status = items[0].get('status', {}).get('streamStatus', '')
                    logger.info(f"liveStream status: {status}")
                    if status == 'active':
                        logger.info("✅ liveStream active — transitioning broadcast to LIVE")
                        self._transition_broadcast_to_live()
                        return
            except Exception as e:
                logger.debug(f"Error polling liveStream: {e}")

            time.sleep(check_interval)
            elapsed += check_interval

        logger.warning(f"Transition monitor timed out after {max_wait}s")

    def _transition_broadcast_to_live(self):
        if self._transition_done:
            return
        try:
            self._yt(self.youtube.liveBroadcasts().transition(
                broadcastStatus='live', id=self.stream.broadcast_id, part='id,status'
            ))
            self._transition_done = True
            logger.info(f"✅ Broadcast {self.stream.broadcast_id} → LIVE")
            try:
                from apps.streaming.models import StreamLog
                StreamLog.objects.create(
                    stream=self.stream, level='INFO',
                    message='Broadcast transitioned to LIVE on YouTube'
                )
            except Exception:
                pass
        except HttpError as e:
            if 'redundantTransition' in str(e.content):
                self._transition_done = True
                logger.info("Broadcast already live (redundantTransition — OK)")
            elif e.resp.status == 403:
                logger.error("403 — enable live streaming at https://www.youtube.com/features")
            else:
                logger.error(f"Transition failed: {e.resp.status} — {e.content}")
        except Exception as e:
            logger.error(f"Broadcast transition error: {e}", exc_info=True)

    # ── FFmpeg start ──────────────────────────────────────────────────────────

    def start_ffmpeg_stream(self):
        try:
            has_local = self.stream.media_files.exists()
            has_playlist = bool(self.stream.playlist_videos)
            logger.info(f"Stream type: local={has_local}, playlist={has_playlist}")

            if has_local:
                return self._start_local_media_stream()
            elif has_playlist:
                return self._start_youtube_playlist_stream()
            else:
                raise Exception("No media files or YouTube playlist attached")

        except Exception as e:
            logger.error(f"FFmpeg start failed: {e}", exc_info=True)
            self._set_error(str(e))
            return None

    def _start_local_media_stream(self):
        media_files = list(self.stream.media_files.all())
        if not media_files:
            raise Exception("No media files found")
        file_paths = download_files_parallel(media_files, self.stream.id)
        concat_path = create_concat_file(media_files, file_paths, self.stream.id, loops=50)
        return self._launch_ffmpeg_process(self._build_concat_cmd(concat_path))

    def _start_youtube_playlist_stream(self):
        if not ytdlp_auth_is_configured():
            logger.warning(
                "yt-dlp auth token not found. Run: python manage.py setup_ytdlp_auth"
            )

        if self.stream.media_files.exists():
            return self._start_local_media_stream()

        videos = _fetch_and_cache_playlist_videos(self.stream)
        return self._start_pipe_stream(videos)

    # ── Per-video FFmpeg pipe stream ──────────────────────────────────────────

    def _start_pipe_stream(self, videos: List[Dict]):
        """
        Launch the feeder thread which streams each video through its own
        fresh FFmpeg process. Returns a dummy PID immediately so the caller
        can mark the stream as running.
        """
        if not videos:
            raise Exception("No videos to stream")
        if not self.stream.stream_url:
            raise Exception("No RTMP URL — call create_broadcast() first")

        _ytdlp_base_cmd()  # raises if yt-dlp not installed

        self._stop_event.clear()
        self._transition_done = False

        # Start feeder in background — it manages its own FFmpeg processes
        feeder = threading.Thread(
            target=self._feed_videos_loop, args=(videos,),
            daemon=True, name=f"feeder-{self.stream.id}"
        )
        feeder.start()

        # Give the feeder a moment to launch the first FFmpeg
        time.sleep(3)

        # Use feeder thread id as a pseudo-pid for status tracking
        pseudo_pid = feeder.ident or os.getpid()
        self._mark_running(pseudo_pid)

        # Start transition monitor (only needs to run once for first video)
        threading.Thread(
            target=self._wait_for_stream_active_and_transition,
            daemon=True, name=f"transition-{self.stream.id}"
        ).start()

        return pseudo_pid

    # ── Main feeder loop (manages per-video FFmpeg processes) ─────────────────

    def _feed_videos_loop(self, videos: List[Dict]):
        """
        Main loop: iterate playlist videos, stream each one with its own
        FFmpeg process. Loops if loop_enabled is True.
        """
        loop_count = 0
        logger.info(f"Playlist contains {len(videos)} videos: {[v.get('title','?') for v in videos]}")
        while not self._stop_event.is_set():
            loop_count += 1
            logger.info(f"▶ Playlist loop #{loop_count} — {len(videos)} videos")

            for v in videos:
                if self._stop_event.is_set():
                    break
                url = v.get('url') or f"https://www.youtube.com/watch?v={v['video_id']}"
                title = v.get('title', url)
                success = self._stream_single_video(url, title)
                if not success and self._stop_event.is_set():
                    break

            if self._stop_event.is_set():
                break

            # Reload loop_enabled from DB in case it was changed
            try:
                from django.apps import apps as django_apps
                Stream = django_apps.get_model('streaming', 'Stream')
                fresh = Stream.objects.get(pk=self.stream.id)
                loop_enabled = fresh.loop_enabled
            except Exception:
                loop_enabled = self.stream.loop_enabled

            if not loop_enabled:
                logger.info("Playlist finished — loop disabled, stopping")
                break

            logger.info("Playlist loop restarting...")
            time.sleep(2)

        logger.info("Feeder loop ended — finalizing stream")
        self._finalize_stream(0)

    # ── Stream one video with its own FFmpeg process ──────────────────────────

    def _stream_single_video(self, url: str, title: str) -> bool:
        """
        Download one video via yt-dlp and pipe it into a fresh FFmpeg process.
        Returns True when done (success or skipped), False only if stop requested.
        """
        COPY_CHUNK = 65536
        auth_args = get_ytdlp_auth_args()
        base_cmd = _ytdlp_base_cmd()

        for attempt in range(1, YTDLP_MAX_RETRIES + 1):
            if self._stop_event.is_set():
                return False

            logger.info(f"▶ [{attempt}/{YTDLP_MAX_RETRIES}] {title}")

            # ── Start fresh FFmpeg for this video ─────────────────────────────
            ffmpeg_cmd = self._build_pipe_cmd()
            popen_kwargs = {
                'stdin': subprocess.PIPE,
                'stdout': subprocess.DEVNULL,
                'stderr': subprocess.PIPE,
            }
            if sys.platform != 'win32':
                popen_kwargs['preexec_fn'] = os.setsid
            else:
                popen_kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP

            try:
                ffmpeg_proc = subprocess.Popen(ffmpeg_cmd, **popen_kwargs)
            except Exception as e:
                logger.error(f"Failed to start FFmpeg: {e}")
                return False

            self.ffmpeg_process = ffmpeg_proc

            # Drain FFmpeg stderr in background
            threading.Thread(
                target=self._drain_stderr,
                args=(ffmpeg_proc.stderr,),
                daemon=True
            ).start()

            # ── Start yt-dlp ──────────────────────────────────────────────────
            yt_cmd = base_cmd + auth_args + [
                '-f', 'best[ext=mp4]/best',
                '--socket-timeout', '60',
                '--fragment-retries', '15',
                '--retries', '10',
                '--file-access-retries', '5',
                '--no-playlist', '--no-warnings', '--no-part',
                '-o', '-',
                url,
            ]

            try:
                yt_proc = subprocess.Popen(
                    yt_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
            except Exception as e:
                logger.error(f"Failed to start yt-dlp: {e}")
                ffmpeg_proc.terminate()
                return False

            # Collect yt-dlp stderr
            stderr_lines = []
            stderr_ready = threading.Event()

            def _collect(proc=yt_proc):
                try:
                    for line in iter(proc.stderr.readline, b''):
                        decoded = line.decode(errors='replace').strip()
                        if decoded:
                            stderr_lines.append(decoded)
                            logger.debug(f"yt-dlp: {decoded}")
                except Exception:
                    pass
                finally:
                    stderr_ready.set()

            threading.Thread(target=_collect, daemon=True).start()

            # ── Pipe yt-dlp stdout → FFmpeg stdin ─────────────────────────────
            bytes_written = 0
            pipe_error = False
            try:
                while True:
                    if self._stop_event.is_set():
                        yt_proc.terminate()
                        try:
                            ffmpeg_proc.stdin.close()
                        except Exception:
                            pass
                        ffmpeg_proc.wait(timeout=5)
                        return False

                    chunk = yt_proc.stdout.read(COPY_CHUNK)
                    if not chunk:
                        break  # yt-dlp finished

                    try:
                        ffmpeg_proc.stdin.write(chunk)
                        ffmpeg_proc.stdin.flush()
                        bytes_written += len(chunk)
                        if bytes_written % (5 * 1024 * 1024) < COPY_CHUNK:
                            logger.debug(f"  Sent {bytes_written / 1024 / 1024:.1f}MB")
                    except (BrokenPipeError, OSError) as e:
                        logger.warning(f"FFmpeg stdin error after {bytes_written} bytes: {e}")
                        yt_proc.terminate()
                        pipe_error = True
                        break

            except Exception as e:
                logger.error(f"Pipe error for '{title}': {e}")
                yt_proc.terminate()
                pipe_error = True

            # Close FFmpeg stdin to signal end of input
            try:
                ffmpeg_proc.stdin.close()
            except Exception:
                pass

            # Wait for yt-dlp to finish
            yt_proc.wait()
            stderr_ready.wait(timeout=5)

            # Wait for FFmpeg to flush and exit cleanly
            try:
                ffmpeg_proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                logger.warning("FFmpeg did not exit in time — terminating")
                try:
                    if sys.platform != 'win32':
                        os.killpg(os.getpgid(ffmpeg_proc.pid), signal.SIGTERM)
                    else:
                        ffmpeg_proc.terminate()
                except Exception:
                    pass

            # ── Decide outcome ────────────────────────────────────────────────
            err_text = ' | '.join(stderr_lines)

            if bytes_written == 0:
                err = ' | '.join(stderr_lines[-3:])
                logger.warning(f"yt-dlp 0 bytes for '{title}' attempt {attempt}: {err}")
                if any(kw in err for kw in ['Sign in', 'bot', 'oauth', 'blocked']):
                    logger.error(
                        "❌ YouTube bot detection.\n"
                        "   Fix: refresh yt-cookies.txt or run setup_ytdlp_auth"
                    )
                if attempt < YTDLP_MAX_RETRIES:
                    time.sleep(YTDLP_RETRY_BACKOFF * attempt)
                else:
                    logger.error(f"Skipping '{title}' after {YTDLP_MAX_RETRIES} attempts")
                    return True  # skip, continue playlist

            elif pipe_error:
                # FFmpeg stdin broke — try again
                if attempt < YTDLP_MAX_RETRIES:
                    logger.warning(f"Pipe error on attempt {attempt}, retrying...")
                    time.sleep(YTDLP_RETRY_BACKOFF)
                else:
                    logger.error(f"Skipping '{title}' after {YTDLP_MAX_RETRIES} pipe errors")
                    return True

            elif yt_proc.returncode != 0:
                # yt-dlp exited with error
                err = ' | '.join(stderr_lines[-5:])
                logger.warning(f"yt-dlp exit {yt_proc.returncode} for '{title}': {err}")
                if attempt < YTDLP_MAX_RETRIES:
                    time.sleep(YTDLP_RETRY_BACKOFF * attempt)
                else:
                    logger.warning(f"Skipping '{title}' after {YTDLP_MAX_RETRIES} attempts")
                    return True

            else:
                # Success
                logger.info(f"✓ Finished: {title} ({bytes_written / 1024 / 1024:.1f} MB)")
                return True

        return True  # exhausted retries, skip video

    def _drain_stderr(self, stderr):
        try:
            for line in iter(stderr.readline, b''):
                if line:
                    if isinstance(line, bytes):
                        line = line.decode(errors='replace')
                    line = line.strip()
                    if not line:
                        continue
                    if any(kw in line.lower() for kw in
                           ['rtmp', 'flv', 'error', 'failed', 'refused',
                            'connected', 'connecting', 'timeout', 'dropping']):
                        logger.info(f"FFmpeg: {line}")
                    else:
                        logger.debug(f"FFmpeg: {line}")
        except Exception:
            pass

    # ── FFmpeg commands ───────────────────────────────────────────────────────

    def _build_concat_cmd(self, concat_path: str) -> list:
        return [
            resolve_ffmpeg_binary(),
            '-re', '-analyzeduration', '10M', '-probesize', '10M',
            '-f', 'concat', '-safe', '0', '-i', concat_path,
            *self._encoding_args(),
        ]

    def _build_pipe_cmd(self) -> list:
        """FFmpeg reads from stdin — fresh process per video, no timestamp issues."""
        return [
            resolve_ffmpeg_binary(),
            '-v', 'warning',       # Less noise; errors still visible
            '-re',                  # Real-time playback rate
            '-thread_queue_size', '1024',
            '-i', 'pipe:0',
            *self._encoding_args(),
        ]

    def _encoding_args(self) -> list:
        return [
            # Video
            '-c:v', 'libx264', '-preset', 'veryfast',
            '-profile:v', 'main', '-level', '4.1',
            '-b:v', '2500k', '-maxrate', '3000k', '-bufsize', '6000k',
            '-g', '60', '-keyint_min', '60',
            '-pix_fmt', 'yuv420p', '-r', '30',
            
            # Audio
            '-c:a', 'aac', '-b:a', '128k', '-ar', '44100', '-ac', '2',
            # Output
            '-f', 'flv',
            '-flvflags', 'no_duration_filesize',
            '-rtbufsize', '8M',
            self.stream.stream_url,
        ]

    def _launch_ffmpeg_process(self, ffmpeg_cmd: list) -> int:
        """Used for local file concat streams."""
        popen_kwargs = {
            'stdin': subprocess.PIPE,
            'stdout': subprocess.DEVNULL,
            'stderr': subprocess.PIPE,
            'universal_newlines': True,
            'bufsize': 1,
        }
        if sys.platform == 'win32':
            popen_kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs['preexec_fn'] = os.setsid

        try:
            self.ffmpeg_process = subprocess.Popen(ffmpeg_cmd, **popen_kwargs)
        except FileNotFoundError:
            raise RuntimeError(f"FFmpeg not found: {ffmpeg_cmd[0]}")

        stderr_lines = []

        def drain():
            try:
                for line in iter(self.ffmpeg_process.stderr.readline, ''):
                    if line.strip():
                        stderr_lines.append(line.strip())
                        logger.info(f"FFmpeg: {line.strip()}")
            except Exception:
                pass

        threading.Thread(target=drain, daemon=True).start()
        time.sleep(3)
        ret = self.ffmpeg_process.poll()
        if ret is not None:
            raise RuntimeError(
                f"FFmpeg exited immediately (code {ret}).\n"
                + '\n'.join(stderr_lines[-10:])
            )

        pid = self.ffmpeg_process.pid
        logger.info(f"FFmpeg alive — PID {pid}")
        self._mark_running(pid)
        self._start_monitor_thread(ffmpeg_cmd)
        threading.Thread(target=self._wait_for_stream_active_and_transition,
                         daemon=True, name=f"transition-{self.stream.id}").start()
        return pid

    def _mark_running(self, pid: int):
        from django.utils import timezone
        self.stream.status = 'running'
        self.stream.process_id = pid
        self.stream.started_at = timezone.now()
        self.stream.process_started_at = timezone.now()
        self.stream.error_message = ''
        self.stream.save()
        StreamCache.set_process_info(self.stream.id, pid, 'running')

    def _start_monitor_thread(self, cmd: list):
        """Only used for local file concat streams."""
        self.monitor_thread = threading.Thread(
            target=self._monitor_ffmpeg, args=(cmd,),
            daemon=True, name=f"monitor-{self.stream.id}"
        )
        self.monitor_thread.start()

    def _monitor_ffmpeg(self, cmd: list):
        """Monitor/restart for local concat streams only."""
        restarts, current_proc = 0, self.ffmpeg_process
        start_time = time.time()

        while restarts < MAX_STREAM_RESTARTS:
            StreamCache.set_process_info(self.stream.id, current_proc.pid, 'running')
            try:
                from django.utils import timezone
                self.stream.last_heartbeat = timezone.now()
                self.stream.save(update_fields=['last_heartbeat'])
            except Exception:
                pass

            try:
                ret = current_proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                continue

            logger.warning(f"FFmpeg exited (code={ret}) — restart #{restarts + 1}")
            if self._stop_event.is_set():
                break
            if time.time() - start_time > 168 * 3600:
                start_time, restarts = time.time(), 0
            if ret in (0, -signal.SIGTERM):
                break

            restarts += 1
            time.sleep(min(60, 5 * restarts))

            popen_kwargs = {
                'stdin': subprocess.PIPE,
                'stdout': subprocess.DEVNULL,
                'stderr': subprocess.PIPE,
                'universal_newlines': True,
                'bufsize': 1,
            }
            if sys.platform == 'win32':
                popen_kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs['preexec_fn'] = os.setsid

            try:
                current_proc = subprocess.Popen(cmd, **popen_kwargs)
                self.ffmpeg_process = current_proc
                from django.utils import timezone
                self.stream.process_id = current_proc.pid
                self.stream.process_started_at = timezone.now()
                self.stream.status = 'running'
                self.stream.save()
                StreamCache.set_process_info(self.stream.id, current_proc.pid, 'running')
                logger.info(f"Restarted — PID {current_proc.pid}")
            except Exception as e:
                logger.error(f"Restart failed: {e}")
                break

        self._finalize_stream(restarts)

    def _finalize_stream(self, restarts: int):
        try:
            from django.utils import timezone
            self.stream.process_id = None
            self.stream.process_started_at = None
            self.stream.last_heartbeat = None
            self.stream.status = 'error' if restarts >= MAX_STREAM_RESTARTS else 'stopped'
            self.stream.error_message = (
                f'FFmpeg crashed after {restarts} restarts'
                if restarts >= MAX_STREAM_RESTARTS else ''
            )
            self.stream.stopped_at = timezone.now()
            self.stream.save()
            StreamCache.delete_process_info(self.stream.id)
            self._cleanup_temp_files()
        except Exception as e:
            logger.error(f"Finalization failed: {e}")

    def _cleanup_temp_files(self):
        try:
            if os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir)
        except Exception as e:
            logger.warning(f"Cleanup failed: {e}")

    def _set_error(self, msg: str):
        self.stream.status = 'error'
        self.stream.error_message = msg[:1000]
        self.stream.save()

    def stop_stream(self) -> bool:
        try:
            self._stop_event.set()

            if self.stream.broadcast_id:
                if not self.youtube:
                    self.authenticate_youtube()
                self._end_youtube_broadcast()

            # Kill current FFmpeg if running
            if self.ffmpeg_process:
                try:
                    self.ffmpeg_process.stdin.close()
                except Exception:
                    pass
                if self.ffmpeg_process.poll() is None:
                    try:
                        self.ffmpeg_process.wait(timeout=GRACEFUL_SHUTDOWN_TIMEOUT)
                    except subprocess.TimeoutExpired:
                        try:
                            if sys.platform == 'win32':
                                self.ffmpeg_process.terminate()
                            else:
                                os.killpg(os.getpgid(self.ffmpeg_process.pid), signal.SIGKILL)
                        except Exception:
                            pass

            elif self.stream.process_id:
                try:
                    if sys.platform == 'win32':
                        subprocess.run(
                            ['taskkill', '/PID', str(self.stream.process_id), '/F'],
                            capture_output=True
                        )
                    else:
                        os.killpg(os.getpgid(self.stream.process_id), signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass

            from django.utils import timezone
            self.stream.status = 'stopped'
            self.stream.stopped_at = timezone.now()
            self.stream.process_id = None
            self.stream.process_started_at = None
            self.stream.last_heartbeat = None
            self.stream.save()
            StreamCache.delete_process_info(self.stream.id)
            self._cleanup_temp_files()
            logger.info(f"Stream {self.stream.id} stopped")
            return True
        except Exception as e:
            logger.error(f"Stop failed: {e}", exc_info=True)
            return False

    def _end_youtube_broadcast(self) -> bool:
        try:
            self._yt(self.youtube.liveBroadcasts().transition(
                broadcastStatus='complete', id=self.stream.broadcast_id, part='id,status'
            ), timeout=BROADCAST_END_TIMEOUT)
            return True
        except HttpError as e:
            if e.resp.status == 403 or 'redundantTransition' in str(e.content):
                return True
            logger.error(f"Error ending broadcast: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to end broadcast: {e}")
            return False

    def get_stream_status(self) -> str:
        info = StreamCache.get_process_info(self.stream.id)
        pid = info.get('pid')
        if pid:
            try:
                os.kill(pid, 0)
                return 'running'
            except OSError:
                pass
        return 'stopped'


# ============ CELERY TASKS ============

@shared_task(time_limit=CELERY_TASK_TIMEOUT, soft_time_limit=CELERY_TASK_TIMEOUT - 300,
             acks_late=True, reject_on_worker_lost=True)
def start_stream_task(stream_id):
    Stream = apps.get_model('streaming', 'Stream')
    stream = Stream.objects.get(pk=stream_id)
    return StreamManager(stream).start_ffmpeg_stream()


@shared_task
def stop_stream_task(stream_id):
    try:
        Stream = apps.get_model('streaming', 'Stream')
        stream = Stream.objects.get(pk=stream_id)
        return StreamManager(stream).stop_stream()
    except Exception as e:
        logger.error(f"Stop task failed: {e}")
        return False


