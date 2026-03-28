from celery import shared_task
from django.utils import timezone
from datetime import timedelta
import logging
import os
import signal

from .models import Stream, StreamLog
from .stream_manager import StreamManager

logger = logging.getLogger(__name__)


@shared_task
def start_scheduled_streams():
    """
    Check for scheduled streams that should start NOW
    Runs every 5 minutes via Celery Beat
    """
    now = timezone.now()
    
    # Find all streams scheduled to start within the last 5 minutes
    scheduled_streams = Stream.objects.filter(
        status='scheduled',
        scheduled_start_time__lte=now,
        is_deleted=False
    ).select_for_update()
    
    logger.info(f"Found {scheduled_streams.count()} scheduled streams to start")
    
    for stream in scheduled_streams:
        try:
            logger.info(f"Starting scheduled stream {stream.id}: {stream.title}")
            
            manager = StreamManager(stream)
            
            # Create YouTube broadcast
            broadcast_id = manager.create_broadcast()
            if not broadcast_id:
                raise Exception("Failed to create YouTube broadcast")
            
            # Start FFmpeg streaming
            process_id = manager.start_ffmpeg_stream()
            if not process_id:
                raise Exception("Failed to start streaming process")
            
            # Update status to running
            stream.status = 'running'
            stream.error_message = ''
            stream.save()
            
            StreamLog.objects.create(
                stream=stream,
                level='INFO',
                message=f'Scheduled stream started automatically'
            )
            
            logger.info(f"✅ Scheduled stream {stream.id} started successfully")
            
        except Exception as e:
            logger.error(f"❌ Failed to start scheduled stream {stream.id}: {str(e)}", exc_info=True)
            
            stream.status = 'error'
            stream.error_message = str(e)
            stream.save()
            
            StreamLog.objects.create(
                stream=stream,
                level='ERROR',
                message=f'Failed to start scheduled stream: {str(e)}'
            )
    
    return f"Started {scheduled_streams.count()} streams"


@shared_task
def check_stream_health():
    """
    Periodic task to check health of all running streams
    Runs every 300 minutes via Celery Beat
    """
    running_streams = Stream.objects.filter(status__in=['running', 'starting'])
    
    for stream in running_streams:
        try:
            manager = StreamManager(stream)
            status = manager.get_stream_status()
            
            # If process is dead but status is running, update it
            if status == 'stopped' and stream.status == 'running':
                stream.status = 'error'
                stream.error_message = 'Stream process died unexpectedly'
                stream.stopped_at = timezone.now()
                stream.process_id = None
                stream.save()
                
                StreamLog.objects.create(
                    stream=stream,
                    level='ERROR',
                    message='Stream process died unexpectedly - auto-detected'
                )
                
                logger.error(f"Stream {stream.id} process died unexpectedly")
            
            # Check if stream has been running too long without issues (health check)
            elif status == 'running' and stream.started_at:
                running_duration = timezone.now() - stream.started_at
                
                # Log every 6 hours that stream is healthy
                if running_duration.total_seconds() % 21600 < 300:  # Within 5 min window
                    StreamLog.objects.create(
                        stream=stream,
                        level='INFO',
                        message=f'Stream healthy - running for {running_duration}'
                    )
                    
        except Exception as e:
            logger.error(f"Error checking stream {stream.id}: {str(e)}")
            StreamLog.objects.create(
                stream=stream,
                level='ERROR',
                message=f'Health check failed: {str(e)}'
            )
    
    logger.info(f"Checked health of {running_streams.count()} streams")
    return f"Checked {running_streams.count()} streams"


@shared_task
def cleanup_old_logs():
    """
    Clean up stream logs older than 30 days
    Runs weekly via Celery Beat
    """
    thirty_days_ago = timezone.now() - timedelta(days=30)
    deleted_count, _ = StreamLog.objects.filter(created_at__lt=thirty_days_ago).delete()
    
    logger.info(f"Cleaned up {deleted_count} old log entries")
    return f"Deleted {deleted_count} old logs"


@shared_task
def start_stream_async(stream_id):
    """
    Async task to start a stream
    """
    try:
        stream = Stream.objects.get(id=stream_id)
        manager = StreamManager(stream)
        
        # Create YouTube broadcast
        broadcast_id = manager.create_broadcast()
        if not broadcast_id:
            raise Exception("Failed to create YouTube broadcast")
        
        # Start FFmpeg streaming
        process_id = manager.start_ffmpeg_stream()
        if not process_id:
            raise Exception("Failed to start streaming process")
        
        StreamLog.objects.create(
            stream=stream,
            level='INFO',
            message='Stream started successfully via async task'
        )
        
        return f"Stream {stream_id} started successfully"
        
    except Stream.DoesNotExist:
        logger.error(f"Stream {stream_id} not found")
        return f"Stream {stream_id} not found"
    except Exception as e:
        logger.error(f"Failed to start stream {stream_id}: {str(e)}")
        
        try:
            stream = Stream.objects.get(id=stream_id)
            stream.status = 'error'
            stream.error_message = str(e)
            stream.save()
            
            StreamLog.objects.create(
                stream=stream,
                level='ERROR',
                message=f'Failed to start stream: {str(e)}'
            )
        except:
            pass
        
        return f"Failed to start stream: {str(e)}"


@shared_task
def stop_stream_async(stream_id):
    """
    Async task to stop a stream
    """
    try:
        stream = Stream.objects.get(id=stream_id)
        manager = StreamManager(stream)
        manager.stop_stream()
        
        StreamLog.objects.create(
            stream=stream,
            level='INFO',
            message='Stream stopped successfully via async task'
        )
        
        return f"Stream {stream_id} stopped successfully"
        
    except Stream.DoesNotExist:
        logger.error(f"Stream {stream_id} not found")
        return f"Stream {stream_id} not found"
    except Exception as e:
        logger.error(f"Failed to stop stream {stream_id}: {str(e)}")
        return f"Failed to stop stream: {str(e)}"


@shared_task
def restart_stream_async(stream_id):
    """
    Async task to restart a stream
    """
    try:
        # Stop the stream first
        stop_result = stop_stream_async(stream_id)
        
        # Wait a bit
        import time
        time.sleep(5)
        
        # Start it again
        start_result = start_stream_async(stream_id)
        
        return f"Stream {stream_id} restarted: Stop={stop_result}, Start={start_result}"
        
    except Exception as e:
        logger.error(f"Failed to restart stream {stream_id}: {str(e)}")
        return f"Failed to restart stream: {str(e)}"

@shared_task(
    time_limit=3600,  # 1 hour timeout
    soft_time_limit=3000
)
def download_playlist_videos_async(stream_id, max_videos=50):
    """
    Async task to download YouTube playlist videos and store as MediaFile objects
    
    Args:
        stream_id: Stream ID to download videos for
        max_videos: Maximum videos to download
    
    Returns:
        Number of videos downloaded
    """
    try:
        stream = Stream.objects.get(id=stream_id)
        logger.info(f"📥 Starting async download for stream {stream_id}")
        
        manager = StreamManager(stream)
        count = manager.download_playlist_videos(max_videos=max_videos)
        
        StreamLog.objects.create(
            stream=stream,
            level='INFO',
            message=f'Downloaded {count} videos from playlist successfully'
        )
        
        logger.info(f"✅ Downloaded {count} videos for stream {stream_id}")
        return f"Downloaded {count} videos successfully"
        
    except Stream.DoesNotExist:
        logger.error(f"Stream {stream_id} not found")
        return f"Stream {stream_id} not found"
    except Exception as e:
        logger.error(f"Failed to download videos for stream {stream_id}: {str(e)}", exc_info=True)
        
        try:
            stream = Stream.objects.get(id=stream_id)
            stream.status = 'error'
            stream.error_message = f"Download failed: {str(e)}"
            stream.save()
            
            StreamLog.objects.create(
                stream=stream,
                level='ERROR',
                message=f'Failed to download videos: {str(e)}'
            )
        except:
            pass
        
        return f"Failed to download videos: {str(e)}"