import subprocess
import os
import signal
from django.conf import settings
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from datetime import datetime, timedelta
import logging
import subprocess, os, signal, psutil
logger = logging.getLogger(__name__)

import os
import shutil
import subprocess
import logging
import time
from typing import List, Optional

logger = logging.getLogger(__name__)

import requests
import tempfile
import threading
from celery import shared_task
from django.apps import apps

def download_s3_file(mediafile):
    url = mediafile.file.url
    resp = requests.get(url, stream=True)
    tmp = tempfile.NamedTemporaryFile(delete=False)
    for chunk in resp.iter_content(chunk_size=1024*1024):
        tmp.write(chunk)
    tmp.close()
    return tmp.name


def _resolve_binary(requested: str) -> str:
    """
    Resolve the ffmpeg binary to use:
      1. If env FFMPEG_PATH is set and executable, use it.
      2. If requested is absolute/relative path and executable, use it.
      3. Otherwise try shutil.which(requested).
    Raises RuntimeError with instructions if not found.
    """
    env_path = os.getenv("FFMPEG_PATH", "").strip()
    if env_path:
        if os.path.isfile(env_path) and os.access(env_path, os.X_OK):
            return env_path
        logger.warning("FFMPEG_PATH is set but not executable: %s", env_path)

    # If requested includes a path component, check it directly
    if os.path.dirname(requested):
        if os.path.isfile(requested) and os.access(requested, os.X_OK):
            return requested
        logger.warning("Requested ffmpeg path not executable: %s", requested)

    # Try PATH
    found = shutil.which(requested)
    if found:
        return found

    raise RuntimeError(
        "ffmpeg binary not found. Install ffmpeg (apt install ffmpeg) "
        "or set a valid FFMPEG_PATH in your .env that points to an executable."
    )

def start_ffmpeg(cmd: List[str], env: Optional[dict] = None, wait_secs: float = 5.0):
    """
    Start ffmpeg subprocess safely. cmd is a list where cmd[0] is usually 'ffmpeg'
    or a path. This function resolves the binary, spawns the process, and waits a
    short time to detect immediate failures. Returns the Popen object if started.
    Raises RuntimeError on failure with stderr/stdout captured when possible.
    """
    if not cmd:
        raise ValueError("cmd must be a non-empty list")

    try:
        cmd0 = _resolve_binary(cmd[0])
    except Exception as e:
        logger.exception("Failed to resolve ffmpeg binary")
        raise

    cmd = [cmd0] + cmd[1:]
    logger.info("Starting ffmpeg: %s", " ".join(cmd))

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1
        )
    except FileNotFoundError as exc:
        logger.exception("Failed to spawn ffmpeg process (file not found)")
        raise RuntimeError(f"Failed to spawn ffmpeg: {exc}") from exc
    except Exception as exc:
        logger.exception("Failed to spawn ffmpeg process")
        raise RuntimeError(f"Failed to spawn ffmpeg: {exc}") from exc

    # Wait a short period for immediate errors (bad args, missing libs, etc.)
    deadline = time.time() + wait_secs
    while time.time() < deadline:
        ret = proc.poll()
        if ret is not None:
            try:
                out, err = proc.communicate(timeout=2)
            except Exception:
                out, err = "", ""
            logger.error("ffmpeg exited immediately (code=%s). stdout=%s stderr=%s", ret, out, err)
            raise RuntimeError(f"ffmpeg exited immediately (code={ret}): {err.strip() or out.strip()}")
        time.sleep(0.1)

    logger.info("ffmpeg started (pid=%s)", proc.pid)
    return proc

class StreamManager:
    def __init__(self, stream):
        self.stream = stream
        self.youtube = None
        
    def authenticate_youtube(self):
        """Authenticate with YouTube API using stored credentials"""
        try:
            youtube_account = self.stream.youtube_account
            credentials = Credentials(
                token=youtube_account.access_token,
                refresh_token=youtube_account.refresh_token,
                token_uri='https://oauth2.googleapis.com/token',
                client_id=settings.GOOGLE_CLIENT_ID,
                client_secret=settings.GOOGLE_CLIENT_SECRET
            )
            
            self.youtube = build('youtube', 'v3', credentials=credentials)
            return True
        except Exception as e:
            logger.error(f"YouTube authentication failed: {str(e)}")
            return False
    
    def create_broadcast(self):
        """Create a YouTube live broadcast"""
        try:
            if not self.youtube:
                if not self.authenticate_youtube():
                    return None
            
            # Create broadcast
            broadcast_response = self.youtube.liveBroadcasts().insert(
                part='snippet,status,contentDetails',
                body={
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
                        'enableAutoStart': True,
                        'enableAutoStop': False,
                        'enableDvr': True,
                        'recordFromStart': True,
                        'enableContentEncryption': False,
                        'enableEmbed': True,
                    }
                }
            ).execute()
            
            broadcast_id = broadcast_response['id']
            
            # Create stream
            stream_response = self.youtube.liveStreams().insert(
                part='snippet,cdn,status',
                body={
                    'snippet': {
                        'title': f"{self.stream.title} - Stream"
                    },
                    'cdn': {
                        'frameRate': 'variable',
                        'ingestionType': 'rtmp',
                        'resolution': 'variable'
                    },
                    'status': {
                        'streamStatus': 'active'
                    }
                }
            ).execute()
            
            stream_id = stream_response['id']
            stream_key = stream_response['cdn']['ingestionInfo']['streamName']
            ingestion_address = stream_response['cdn']['ingestionInfo']['ingestionAddress']
            
            # Bind broadcast to stream
            self.youtube.liveBroadcasts().bind(
                part='id,contentDetails',
                id=broadcast_id,
                streamId=stream_id
            ).execute()
            
            # Update stream model
            self.stream.broadcast_id = broadcast_id
            self.stream.stream_key = stream_key
            self.stream.stream_url = f"{ingestion_address}/{stream_key}"
            self.stream.save()
            
            return broadcast_id
            
        except Exception as e:
            logger.error(f"Failed to create broadcast: {str(e)}")
            self.stream.status = 'error'
            self.stream.error_message = str(e)
            self.stream.save()
            return None
            
    def start_ffmpeg_stream(self):
        """Start FFmpeg streaming process"""
        try:
            media_files = self.stream.media_files.all()
            if not media_files:
                raise Exception("No media files attached to stream")
            
            # Create input file list for FFmpeg concat
            input_list_path = f'/tmp/stream_{self.stream.id}_inputs.txt'
            with open(input_list_path, 'w') as f:
                for media_file in media_files:
                    file_path = download_s3_file(media_file)

                    # write input list entries (audio/video treated same here)
                    f.write(f"file '{file_path}'\n")
            
            # Create a single MPEG-TS concat with monotonic timestamps (genpts) to avoid PTS resets
            temp_concat = f'/tmp/stream_{self.stream.id}_concat.ts'
            concat_cmd = [
                settings.FFMPEG_PATH,
                '-f', 'concat',
                '-safe', '0',
                '-i', input_list_path,
                '-fflags', '+genpts',
                '-avoid_negative_ts', 'make_zero',
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-b:v', '3000k',
                '-maxrate', '3000k',
                '-bufsize', '6000k',
                '-g', '50',
                '-pix_fmt', 'yuv420p',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-ar', '44100',
                '-f', 'mpegts',
                temp_concat
            ]

            logger.info('Creating MPEG-TS concatenated temp file: %s', temp_concat)
            proc_concat = subprocess.run(concat_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if proc_concat.returncode != 0:
                # Try stream copy (may fail if codecs/params differ)
                copy_concat_cmd = [
                    settings.FFMPEG_PATH,
                    '-f', 'concat',
                    '-safe', '0',
                    '-i', input_list_path,
                    '-c', 'copy',
                    '-f', 'mpegts',
                    temp_concat
                ]
                proc_copy = subprocess.run(copy_concat_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if proc_copy.returncode == 0:
                    logger.info('TS concat created with stream copy')
                else:
                    logger.warning('TS stream copy concat failed, re-encode stderr: %s', proc_copy.stderr)
                    logger.error('Concat creation failed (re-encode): %s', proc_concat.stderr)
                    raise RuntimeError(f'Failed to create TS concat file: {proc_concat.stderr}\n{proc_copy.stderr}')

            # FFmpeg command for streaming: use TS input with genpts to preserve monotonic timestamps
            ffmpeg_cmd = [
                settings.FFMPEG_PATH,
                '-re',
                '-fflags', '+genpts',
                '-stream_loop', '-1' if self.stream.loop_enabled else '0',
                '-i', temp_concat,
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-b:v', '3000k',
                '-maxrate', '3000k',
                '-bufsize', '6000k',
                '-pix_fmt', 'yuv420p',
                '-g', '50',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-ar', '44100',
                '-f', 'flv',
                self.stream.stream_url
            ]

            # Start FFmpeg process and stream stderr to logger so we can see errors when it stops
            process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid,
                universal_newlines=True
            )

            def _log_stream(pipe, level=logging.INFO):
                try:
                    for line in iter(pipe.readline, ''):
                        if line:
                            logger.log(level, line.strip())
                except Exception:
                    pass

            t = threading.Thread(target=_log_stream, args=(process.stderr, logging.ERROR), daemon=True)
            t.start()

            # Monitor thread: watch ffmpeg, attempt auto-restarts, update DB, and cleanup
            def _monitor_ffmpeg(p, cmd, max_restarts=3):
                restarts = 0
                cur_proc = p
                while True:
                    ret = cur_proc.wait()
                    logger.error('ffmpeg exited with code %s for stream %s (attempt %s)', ret, self.stream.id, restarts)

                    # If clean stop (ret==0) or we've exhausted restarts, mark and exit loop
                    if ret == 0 or restarts >= max_restarts:
                        try:
                            self.stream.process_id = None
                            if ret == 0:
                                self.stream.status = 'stopped'
                                self.stream.error_message = ''
                            else:
                                self.stream.status = 'error'
                                self.stream.error_message = f'ffmpeg exited with code {ret} after {restarts} restarts'
                            self.stream.stopped_at = datetime.now()
                            self.stream.save(update_fields=['process_id', 'status', 'stopped_at', 'error_message'])
                        except Exception as e:
                            logger.exception('Failed to update stream after ffmpeg exit: %s', e)
                        break

                    # Attempt restart
                    restarts += 1
                    backoff = 5 * restarts
                    logger.info('Restarting ffmpeg for stream %s (attempt %s) after %ss', self.stream.id, restarts, backoff)
                    time.sleep(backoff)
                    try:
                        new_proc = subprocess.Popen(
                            cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            preexec_fn=os.setsid,
                            universal_newlines=True
                        )
                        cur_proc = new_proc
                        # update DB process_id
                        try:
                            self.stream.process_id = new_proc.pid
                            self.stream.status = 'running'
                            self.stream.save(update_fields=['process_id', 'status'])
                        except Exception:
                            logger.exception('Failed to update stream process_id on restart')

                        # spawn a stderr logger for this restarted process
                        t2 = threading.Thread(target=_log_stream, args=(new_proc.stderr, logging.ERROR), daemon=True)
                        t2.start()
                    except Exception as e:
                        logger.exception('Failed to restart ffmpeg for stream %s: %s', self.stream.id, e)
                        continue

                # cleanup temp files when final
                try:
                    if os.path.exists(temp_concat):
                        os.remove(temp_concat)
                    if os.path.exists(input_list_path):
                        os.remove(input_list_path)
                except Exception:
                    logger.exception('Failed to remove temp files for stream %s', self.stream.id)

            monitor_t = threading.Thread(target=_monitor_ffmpeg, args=(process, ffmpeg_cmd), daemon=True)
            monitor_t.start()
            
            self.stream.process_id = process.pid
            self.stream.status = 'running'
            self.stream.started_at = datetime.now()
            self.stream.save()
            
            logger.info(f"Stream {self.stream.id} started with PID {process.pid}")
            return process.pid
            
        except Exception as e:
            logger.error(f"Failed to start FFmpeg: {str(e)}")
            self.stream.status = 'error'
            self.stream.error_message = str(e)
            self.stream.save()
            return None
    def stop_ffmpeg_gracefully(self, pid):
        """Stop FFmpeg process group"""
        try:
            import sys
            if sys.platform != 'win32':
                # Linux/Mac: Kill entire process group
                os.killpg(os.getpgid(pid), signal.SIGTERM)
                time.sleep(2)
                # Force kill if still alive
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass  # Already dead
            else:
                # Windows: Use taskkill
                subprocess.call(['taskkill', '/F', '/T', '/PID', str(pid)])
        
            return True
        except Exception as e:
            logger.error(f"Failed to stop process group: {e}")
            return False
    def stop_stream(self):
        """Completely stop FFmpeg and end YouTube broadcast cleanly"""
        try:
            # 1️⃣ Kill FFmpeg process locally
            if self.stream.process_id:
                success = self.stop_ffmpeg_gracefully(self.stream.process_id)
                if success:
                    logger.info(f"Stopped FFmpeg process for stream {self.stream.id}")
                else:
                    logger.warning(f"Manual termination required for PID {self.stream.process_id}")
                self.stream.process_id = None

            # 2️⃣ Make sure YouTube API client is ready
            if not hasattr(self, 'youtube') or not self.youtube:
                self.authenticate_youtube()

            # 3️⃣ Mark broadcast complete on YouTube
            if self.youtube and self.stream.broadcast_id:
                try:
                    self.youtube.liveBroadcasts().transition(
                        broadcastStatus='complete',
                        id=self.stream.broadcast_id,
                        part='status'
                    ).execute()
                    logger.info(f"YouTube broadcast {self.stream.broadcast_id} ended successfully")
                except Exception as e:
                    logger.error(f"YouTube broadcast completion failed: {e}")

            # 4️⃣ Update database
            self.stream.status = 'stopped'
            self.stream.stopped_at = datetime.now()
            self.stream.save(update_fields=['status', 'stopped_at', 'process_id'])
            return True

        except Exception as e:
            self.stream.status = 'error'
            self.stream.error_message = str(e)
            self.stream.save(update_fields=['status', 'error_message'])
            logger.error(f"Error stopping stream {self.stream.id}: {e}")
            return False        

    


# Module-level task that Celery should call. It looks up the Stream model by id,
# creates a StreamManager and runs the instance method. This avoids decorating
# the instance method with @shared_task which causes the missing-self error.
@shared_task(time_limit=86400, soft_time_limit=86100)
def start_ffmpeg_stream_task(stream_id):
    try:
        Stream = apps.get_model('streaming', 'Stream')
        stream = Stream.objects.get(pk=stream_id)
        manager = StreamManager(stream)
        return manager.start_ffmpeg_stream()
    except Exception as e:
        logger.exception('Failed to start ffmpeg stream task for id %s: %s', stream_id, e)
        raise
    
    def stop_ffmpeg_gracefully(self, pid):
        """Stop FFmpeg process group"""
        try:
            import sys
            if sys.platform != 'win32':
                # Linux/Mac: Kill entire process group
                os.killpg(os.getpgid(pid), signal.SIGTERM)
                time.sleep(2)
                # Force kill if still alive
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass  # Already dead
            else:
                # Windows: Use taskkill
                subprocess.call(['taskkill', '/F', '/T', '/PID', str(pid)])
        
            return True
        except Exception as e:
            logger.error(f"Failed to stop process group: {e}")
            return False
    '''
    def stop_stream(self):
        """Stop the streaming process"""
        try:
            if self.stream.process_id:
                success = self.stop_ffmpeg_gracefully(self.stream.process_id)
                if success:
                    logger.info(f"Successfully stopped stream {self.stream.id} with PID {self.stream.process_id}")
                else:
                    logger.warning(f"Forced kill of stream {self.stream.id}")   
            
            # Stop YouTube broadcast
            if self.youtube and self.stream.broadcast_id:
                try:
                    self.youtube.liveBroadcasts().transition(
                        part='status',
                        id=self.stream.broadcast_id,
                        broadcastStatus='complete'
                    ).execute()
                except Exception as e:
                    logger.error(f"Failed to complete broadcast: {str(e)}")
            
            self.stream.status = 'stopped'
            self.stream.stopped_at = datetime.now()
            self.stream.process_id = None
            self.stream.save()
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to stop stream: {str(e)}")
            self.stream.status = 'error'
            self.stream.error_message = str(e)
            self.stream.save()
            return False
    
    def get_stream_status(self):
        """Check if the stream is still running"""
        if not self.stream.process_id:
            return 'stopped'
        
        try:
            # Check if process is running
            os.kill(self.stream.process_id, 0)
            return 'running'
        except ProcessLookupError:
            return 'stopped'
    '''
    def stop_stream(self):
        """Completely stop FFmpeg and end YouTube broadcast cleanly"""
        try:
            # 1️⃣ Kill FFmpeg process locally
            if self.stream.process_id:
                success = self.stop_ffmpeg_gracefully(self.stream.process_id)
                if success:
                    logger.info(f"Stopped FFmpeg process for stream {self.stream.id}")
                else:
                    logger.warning(f"Manual termination required for PID {self.stream.process_id}")
                self.stream.process_id = None

            # 2️⃣ Make sure YouTube API client is ready
            if not hasattr(self, 'youtube') or not self.youtube:
                self.authenticate_youtube()

            # 3️⃣ Mark broadcast complete on YouTube
            if self.youtube and self.stream.broadcast_id:
                try:
                    self.youtube.liveBroadcasts().transition(
                        broadcastStatus='complete',
                        id=self.stream.broadcast_id,
                        part='status'
                    ).execute()
                    logger.info(f"YouTube broadcast {self.stream.broadcast_id} ended successfully")
                except Exception as e:
                    logger.error(f"YouTube broadcast completion failed: {e}")

            # 4️⃣ Update database
            self.stream.status = 'stopped'
            self.stream.stopped_at = datetime.now()
            self.stream.save(update_fields=['status', 'stopped_at', 'process_id'])
            return True

        except Exception as e:
            self.stream.status = 'error'
            self.stream.error_message = str(e)
            self.stream.save(update_fields=['status', 'error_message'])
            logger.error(f"Error stopping stream {self.stream.id}: {e}")
            return False
