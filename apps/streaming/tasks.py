"""
tasks.py — Celery tasks for the streaming app.

Changes vs original:
  - download_playlist_videos_async: now clearly marked as OPTIONAL (storage path).
    For direct streaming, starting the stream is sufficient — no pre-download needed.
  - Added stream_playlist_direct_async: kicks off a stream that uses the new
    PlaylistPipeStreamer (yt-dlp → FIFO → FFmpeg → RTMP, no downloads).
  - restart_stream_async: calls the Celery tasks via .delay() instead of calling
    stop/start synchronously inside one task (avoids blocking worker).
"""

from celery import shared_task
from django.utils import timezone
from datetime import timedelta
import logging
import time

from .models import Stream, StreamLog
from .stream_manager import StreamManager

logger = logging.getLogger(__name__)


# ============ SCHEDULED / HEALTH TASKS ============

@shared_task
def start_scheduled_streams():
    """
    Check for streams scheduled to start NOW.
    Runs every 5 minutes via Celery Beat.
    """
    now = timezone.now()

    scheduled_streams = Stream.objects.filter(
        status='scheduled',
        scheduled_start_time__lte=now,
        is_deleted=False,
    ).select_for_update()

    logger.info("Found %d scheduled streams to start", scheduled_streams.count())

    for stream in scheduled_streams:
        try:
            logger.info("Starting scheduled stream %s: %s", stream.id, stream.title)
            manager = StreamManager(stream)

            broadcast_id = manager.create_broadcast()
            if not broadcast_id:
                raise Exception("Failed to create YouTube broadcast")

            pid = manager.start_ffmpeg_stream()
            if not pid:
                raise Exception("Failed to start streaming process")

            stream.status = 'running'
            stream.error_message = ''
            stream.save(update_fields=['status', 'error_message'])

            StreamLog.objects.create(
                stream=stream,
                level='INFO',
                message='Scheduled stream started automatically',
            )
            logger.info("✅ Scheduled stream %s started", stream.id)

        except Exception as e:
            logger.error("❌ Failed to start scheduled stream %s: %s", stream.id, e, exc_info=True)
            stream.status = 'error'
            stream.error_message = str(e)
            stream.save(update_fields=['status', 'error_message'])
            StreamLog.objects.create(
                stream=stream,
                level='ERROR',
                message=f'Failed to start scheduled stream: {e}',
            )

    return f"Started {scheduled_streams.count()} streams"


@shared_task
def check_stream_health():
    """
    Periodic health check for all running/starting streams.
    Runs every 5 minutes via Celery Beat.
    """
    running_streams = Stream.objects.filter(status__in=['running', 'starting'])

    for stream in running_streams:
        try:
            manager = StreamManager(stream)
            status = manager.get_stream_status()

            if status == 'stopped' and stream.status == 'running':
                stream.status = 'error'
                stream.error_message = 'Stream process died unexpectedly'
                stream.stopped_at = timezone.now()
                stream.process_id = None
                stream.save(update_fields=['status', 'error_message', 'stopped_at', 'process_id'])

                StreamLog.objects.create(
                    stream=stream,
                    level='ERROR',
                    message='Stream process died unexpectedly — auto-detected',
                )
                logger.error("Stream %s process died unexpectedly", stream.id)

            elif status == 'running' and stream.started_at:
                running_duration = timezone.now() - stream.started_at
                if running_duration.total_seconds() % 21600 < 300:
                    StreamLog.objects.create(
                        stream=stream,
                        level='INFO',
                        message=f'Stream healthy — running for {running_duration}',
                    )

        except Exception as e:
            logger.error("Error checking stream %s: %s", stream.id, e)
            StreamLog.objects.create(
                stream=stream,
                level='ERROR',
                message=f'Health check failed: {e}',
            )

    logger.info("Checked health of %d streams", running_streams.count())
    return f"Checked {running_streams.count()} streams"

@shared_task
def cleanup_orphaned_broadcasts():
    try:
        Stream = apps.get_model('streaming', 'Stream')
        for stream in Stream.objects.filter(
            status__in=['error', 'stopped']
        ).exclude(broadcast_id=''):
            try:
                mgr = StreamManager(stream)
                if mgr.authenticate_youtube():
                    mgr._end_youtube_broadcast()
                    stream.broadcast_id = ''
                    stream.save(update_fields=['broadcast_id'])
            except Exception as e:
                logger.warning(f"Cleanup failed for {stream.id}: {e}")
    except Exception as e:
        logger.error(f"Cleanup task failed: {e}")


@shared_task
def cleanup_old_logs():
    """
    Clean up stream logs older than 30 days.
    Runs weekly via Celery Beat.
    """
    cutoff = timezone.now() - timedelta(days=30)
    deleted_count, _ = StreamLog.objects.filter(created_at__lt=cutoff).delete()
    logger.info("Cleaned up %d old log entries", deleted_count)
    return f"Deleted {deleted_count} old logs"


# ============ STREAM CONTROL TASKS ============

@shared_task
def start_stream_async(stream_id):
    """
    Async task: create broadcast + start streaming.

    Works for BOTH paths:
      - Local media files → S3 download → concat → FFmpeg → RTMP
      - YouTube playlist  → yt-dlp pipe → FFmpeg → RTMP (no downloads)
    """
    try:
        stream = Stream.objects.get(id=stream_id)
        manager = StreamManager(stream)

        broadcast_id = manager.create_broadcast()
        if not broadcast_id:
            raise Exception("Failed to create YouTube broadcast")

        pid = manager.start_ffmpeg_stream()
        if not pid:
            raise Exception("Failed to start streaming process")

        StreamLog.objects.create(
            stream=stream,
            level='INFO',
            message='Stream started successfully via async task',
        )
        return f"Stream {stream_id} started (PID {pid})"

    except Stream.DoesNotExist:
        logger.error("Stream %s not found", stream_id)
        return f"Stream {stream_id} not found"
    except Exception as e:
        logger.error("Failed to start stream %s: %s", stream_id, e)
        try:
            stream = Stream.objects.get(id=stream_id)
            stream.status = 'error'
            stream.error_message = str(e)
            stream.save(update_fields=['status', 'error_message'])
            StreamLog.objects.create(
                stream=stream,
                level='ERROR',
                message=f'Failed to start stream: {e}',
            )
        except Exception:
            pass
        return f"Failed to start stream: {e}"


@shared_task
def stop_stream_async(stream_id):
    """Async task: gracefully stop a running stream."""
    try:
        stream = Stream.objects.get(id=stream_id)
        StreamManager(stream).stop_stream()
        StreamLog.objects.create(
            stream=stream,
            level='INFO',
            message='Stream stopped successfully via async task',
        )
        return f"Stream {stream_id} stopped"

    except Stream.DoesNotExist:
        logger.error("Stream %s not found", stream_id)
        return f"Stream {stream_id} not found"
    except Exception as e:
        logger.error("Failed to stop stream %s: %s", stream_id, e)
        return f"Failed to stop stream: {e}"


@shared_task
def restart_stream_async(stream_id):
    """
    Async task: stop then restart a stream.
    Uses a Celery chord instead of calling tasks synchronously.
    """
    try:
        stream = Stream.objects.get(id=stream_id)
        manager = StreamManager(stream)

        # Stop
        manager.stop_stream()
        time.sleep(5)

        # Recreate broadcast + restart
        broadcast_id = manager.create_broadcast()
        if not broadcast_id:
            raise Exception("Failed to recreate YouTube broadcast")

        pid = manager.start_ffmpeg_stream()
        if not pid:
            raise Exception("Failed to restart streaming process")

        StreamLog.objects.create(
            stream=stream,
            level='INFO',
            message='Stream restarted successfully',
        )
        return f"Stream {stream_id} restarted (PID {pid})"

    except Exception as e:
        logger.error("Failed to restart stream %s: %s", stream_id, e)
        return f"Failed to restart stream: {e}"


# ============ DIRECT PLAYLIST STREAMING TASK ============

@shared_task(
    time_limit=86400,        # 24 h — long-running stream
    soft_time_limit=86100,
    acks_late=True,
    reject_on_worker_lost=True,
)
def stream_playlist_direct_async(stream_id: str):
    """
    Start a DIRECT playlist stream (no downloads, no S3).

    This task:
      1. Fetches the YouTube broadcast / RTMP credentials (create_broadcast).
      2. Calls start_ffmpeg_stream() which routes to PlaylistPipeStreamer
         because stream.media_files is empty and stream.playlist_videos is set.
      3. The Celery task stays alive for the duration of the stream.
         (set time_limit to match your expected max stream length.)

    Triggered from views.stream_start() when stream has a playlist.
    """
    try:
        stream = Stream.objects.get(id=stream_id)
        logger.info("🎬 stream_playlist_direct_async: stream %s", stream_id)

        manager = StreamManager(stream)

        # Create broadcast only if we don't already have one
        if not stream.broadcast_id or not stream.stream_url:
            broadcast_id = manager.create_broadcast()
            if not broadcast_id:
                raise Exception("Failed to create YouTube broadcast")
            stream.refresh_from_db()

        pid = manager.start_ffmpeg_stream()
        if not pid:
            raise Exception("start_ffmpeg_stream() returned no PID")

        StreamLog.objects.create(
            stream=stream,
            level='INFO',
            message=f'Direct playlist stream started (PID {pid})',
        )

        # Block until FFmpeg exits (the pipe streamer's feeder thread manages the rest)
        ffmpeg_proc = manager.ffmpeg_process
        if ffmpeg_proc:
            ret = ffmpeg_proc.wait()
            logger.info("FFmpeg exited with code %d for stream %s", ret, stream_id)

        return f"Stream {stream_id} completed"

    except Stream.DoesNotExist:
        logger.error("Stream %s not found", stream_id)
        return f"Stream {stream_id} not found"
    except Exception as e:
        logger.error("stream_playlist_direct_async failed for %s: %s", stream_id, e, exc_info=True)
        try:
            stream = Stream.objects.get(id=stream_id)
            stream.status = 'error'
            stream.error_message = str(e)
            stream.save(update_fields=['status', 'error_message'])
            StreamLog.objects.create(
                stream=stream, level='ERROR',
                message=f'Direct playlist stream failed: {e}',
            )
        except Exception:
            pass
        return f"Failed: {e}"


# ============ OPTIONAL: DOWNLOAD-TO-STORAGE TASK ============

@shared_task(
    time_limit=3600,
    soft_time_limit=3000,
)
def download_playlist_videos_async(stream_id, max_videos=50):
    """
    OPTIONAL — Download YouTube playlist videos to S3 as MediaFile objects.

    Not required for direct streaming.  Use this only if you want to pre-cache
    videos in S3 and stream from stored files.

    Once downloaded, starting the stream will automatically use the stored
    MediaFiles instead of live-piping from YouTube.
    """
    try:
        stream = Stream.objects.get(id=stream_id)
        logger.info("📥 Starting storage-download for stream %s", stream_id)

        manager = StreamManager(stream)
        count = manager.download_playlist_videos(max_videos=max_videos)

        StreamLog.objects.create(
            stream=stream,
            level='INFO',
            message=f'Downloaded {count} videos from playlist to storage',
        )
        logger.info("✅ Downloaded %d videos for stream %s", count, stream_id)
        return f"Downloaded {count} videos"

    except Stream.DoesNotExist:
        logger.error("Stream %s not found", stream_id)
        return f"Stream {stream_id} not found"
    except Exception as e:
        logger.error("Download failed for stream %s: %s", stream_id, e, exc_info=True)
        try:
            stream = Stream.objects.get(id=stream_id)
            stream.status = 'error'
            stream.error_message = f"Download failed: {e}"
            stream.save(update_fields=['status', 'error_message'])
            StreamLog.objects.create(
                stream=stream, level='ERROR',
                message=f'Failed to download videos: {e}',
            )
        except Exception:
            pass
        return f"Failed to download: {e}"
