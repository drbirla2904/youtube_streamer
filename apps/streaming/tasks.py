"""
tasks.py — Celery tasks for the streaming app.

Scheduling support:
  start_scheduled_streams  — handles 'once' one-time AND 'daily' recurring streams.
                             Also auto-stops streams that have passed their end time.
  _start_stream_now        — shared helper: create broadcast + start FFmpeg.
  _stop_and_maybe_upload   — stop stream; respects auto_upload_after_end flag.

Auto-upload:
  auto_upload_after_end=True  →  recordFromStart=True is set in create_broadcast()
                                  inside stream_manager.py — YouTube natively saves
                                  the recording when the broadcast ends.
                                  No separate upload task is needed.
"""

from celery import shared_task
from django.utils import timezone
from django.conf import settings
from datetime import timedelta
import logging
import time

from .models import Stream, StreamLog
from .stream_manager import StreamManager

logger = logging.getLogger(__name__)


# ============ HELPERS ============

def _start_stream_now(stream: Stream):
    """Create broadcast + start FFmpeg for a scheduled stream."""
    try:
        manager = StreamManager(stream)
        broadcast_id = manager.create_broadcast()
        if not broadcast_id:
            raise Exception("Failed to create YouTube broadcast")
        pid = manager.start_ffmpeg_stream()
        if not pid:
            raise Exception("Failed to start FFmpeg process")
        stream.status = 'running'
        stream.error_message = ''
        stream.save(update_fields=['status', 'error_message'])
        StreamLog.objects.create(
            stream=stream, level='INFO',
            message=f'Stream auto-started by scheduler ({stream.schedule_type})',
        )
        logger.info("✅ Scheduled stream %s started (type=%s)", stream.id, stream.schedule_type)
    except Exception as e:
        logger.error("❌ Scheduled stream %s failed: %s", stream.id, e, exc_info=True)
        stream.status = 'error'
        stream.error_message = str(e)
        stream.save(update_fields=['status', 'error_message'])
        StreamLog.objects.create(
            stream=stream, level='ERROR',
            message=f'Scheduler failed to start stream: {e}',
        )


def _stop_and_maybe_upload(stream: Stream):
    """
    Stop a running stream at its scheduled end time.
    If auto_upload_after_end=True the broadcast was already created with
    recordFromStart=True, so YouTube handles saving the recording natively
    — nothing extra needed here.
    """
    try:
        StreamManager(stream).stop_stream()
        msg = 'Stream auto-stopped at scheduled end time'
        if stream.auto_upload_after_end:
            msg += ' · Recording will be saved to YouTube automatically'
        StreamLog.objects.create(stream=stream, level='INFO', message=msg)
        logger.info("🛑 Auto-stopped stream %s (auto_upload=%s)", stream.id, stream.auto_upload_after_end)
    except Exception as e:
        logger.error("Auto-stop failed for stream %s: %s", stream.id, e, exc_info=True)
        StreamLog.objects.create(
            stream=stream, level='ERROR',
            message=f'Auto-stop failed: {e}',
        )


# ============ SCHEDULED / HEALTH TASKS ============
from django.utils import timezone
import pytz

@shared_task
def start_scheduled_streams():
    now = timezone.now()  # UTC
    started = stopped = 0

    # ── 1. One-time streams ───────────────────────────────────────────────────
    for stream in Stream.objects.filter(
        status='scheduled',
        schedule_type='once',
        scheduled_start_time__lte=now,
        is_deleted=False,
    ):
        _start_stream_now(stream)
        started += 1

    # ── 2. Daily streams — compare HH:MM in user's local timezone ─────────────
    for stream in Stream.objects.filter(
        schedule_type='daily',
        is_deleted=False,
    ).exclude(
        daily_start_time=''
    ).exclude(
        status__in=['running', 'starting', 'stopping']
    ):
        try:
            user_tz = pytz.timezone(stream.user_timezone or 'UTC')
        except Exception:
            user_tz = pytz.UTC
        local_now = now.astimezone(user_tz)
        local_hhmm = local_now.strftime('%H:%M')
        if stream.daily_start_time == local_hhmm:
            _start_stream_now(stream)
            started += 1

    # ── 3. Auto-stop — once streams ───────────────────────────────────────────
    for stream in Stream.objects.filter(
        status='running',
        schedule_type='once',
        scheduled_end_time__lte=now,
        is_deleted=False,
    ):
        _stop_and_maybe_upload(stream)
        stopped += 1

    # ── 4. Auto-stop — daily streams ─────────────────────────────────────────
    for stream in Stream.objects.filter(
        status='running',
        schedule_type='daily',
        is_deleted=False,
    ).exclude(daily_end_time=''):
        try:
            user_tz = pytz.timezone(stream.user_timezone or 'UTC')
        except Exception:
            user_tz = pytz.UTC
        local_now = now.astimezone(user_tz)
        local_hhmm = local_now.strftime('%H:%M')
        if stream.daily_end_time == local_hhmm:
            _stop_and_maybe_upload(stream)
            stopped += 1

    logger.info("Scheduler tick: started=%d stopped=%d", started, stopped)
    return f"Started {started}, stopped {stopped}"


@shared_task
def check_stream_health():
    """
    Periodic health check for all running/starting streams.
    Runs every hour via Celery Beat.
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
                    stream=stream, level='ERROR',
                    message='Stream process died unexpectedly — auto-detected',
                )
                logger.error("Stream %s process died unexpectedly", stream.id)

            elif status == 'running' and stream.started_at:
                running_duration = timezone.now() - stream.started_at
                if running_duration.total_seconds() % 21600 < 300:
                    StreamLog.objects.create(
                        stream=stream, level='INFO',
                        message=f'Stream healthy — running for {running_duration}',
                    )

        except Exception as e:
            logger.error("Error checking stream %s: %s", stream.id, e)
            StreamLog.objects.create(
                stream=stream, level='ERROR',
                message=f'Health check failed: {e}',
            )

    logger.info("Checked health of %d streams", running_streams.count())
    return f"Checked {running_streams.count()} streams"


@shared_task
def cleanup_orphaned_broadcasts():
    from django.apps import apps
    try:
        StreamModel = apps.get_model('streaming', 'Stream')
        for stream in StreamModel.objects.filter(
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
    """Clean up stream logs older than 30 days. Runs weekly via Celery Beat."""
    cutoff = timezone.now() - timedelta(days=30)
    deleted_count, _ = StreamLog.objects.filter(created_at__lt=cutoff).delete()
    logger.info("Cleaned up %d old log entries", deleted_count)
    return f"Deleted {deleted_count} old logs"


# ============ STREAM CONTROL TASKS ============

@shared_task
def start_stream_async(stream_id):
    """Async task: create broadcast + start streaming."""
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
            stream=stream, level='INFO',
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
                stream=stream, level='ERROR',
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
            stream=stream, level='INFO',
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
    """Async task: stop then restart a stream."""
    try:
        stream = Stream.objects.get(id=stream_id)
        manager = StreamManager(stream)

        manager.stop_stream()
        time.sleep(5)

        broadcast_id = manager.create_broadcast()
        if not broadcast_id:
            raise Exception("Failed to recreate YouTube broadcast")

        pid = manager.start_ffmpeg_stream()
        if not pid:
            raise Exception("Failed to restart streaming process")

        StreamLog.objects.create(
            stream=stream, level='INFO',
            message='Stream restarted successfully',
        )
        return f"Stream {stream_id} restarted (PID {pid})"

    except Exception as e:
        logger.error("Failed to restart stream %s: %s", stream_id, e)
        return f"Failed to restart stream: {e}"


# ============ DIRECT PLAYLIST STREAMING TASK ============

@shared_task(
    time_limit=86400,
    soft_time_limit=86100,
    acks_late=True,
    reject_on_worker_lost=True,
)
def stream_playlist_direct_async(stream_id: str):
    """
    Start a DIRECT playlist stream (no downloads, no S3).
    Triggered from views.stream_start() when stream has a playlist.
    """
    try:
        stream = Stream.objects.get(id=stream_id)
        logger.info("🎬 stream_playlist_direct_async: stream %s", stream_id)

        manager = StreamManager(stream)

        if not stream.broadcast_id or not stream.stream_url:
            broadcast_id = manager.create_broadcast()
            if not broadcast_id:
                raise Exception("Failed to create YouTube broadcast")
            stream.refresh_from_db()

        pid = manager.start_ffmpeg_stream()
        if not pid:
            raise Exception("start_ffmpeg_stream() returned no PID")

        StreamLog.objects.create(
            stream=stream, level='INFO',
            message=f'Direct playlist stream started (PID {pid})',
        )

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
    Not required for direct streaming.
    """
    try:
        stream = Stream.objects.get(id=stream_id)
        logger.info("📥 Starting storage-download for stream %s", stream_id)

        manager = StreamManager(stream)
        count = manager.download_playlist_videos(max_videos=max_videos)

        StreamLog.objects.create(
            stream=stream, level='INFO',
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
