"""
celery.py — Celery application for youtube_streamer.

Changes vs original:
  - Added cleanup_orphaned_broadcasts to beat schedule (runs hourly alongside
    health check, cleans up stuck YouTube broadcasts from crashed streams).
  - Added stream_playlist_direct_async to task_routes under 'streaming' queue.
  - Visibility timeout set high (25 h) so Redis doesn't re-queue a running
    24-hour stream task.
  - worker_max_tasks_per_child kept low on the streaming queue so FFmpeg
    sub-processes don't accumulate in long-lived workers.
  - Removed debug_task (not needed in production).
"""

import os
import logging
from celery import Celery
from celery.schedules import crontab

logger = logging.getLogger(__name__)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('youtube_streamer')

# Pull all CELERY_* settings from Django settings automatically
app.config_from_object('django.conf:settings', namespace='CELERY')

# Auto-discover tasks from all registered apps
app.autodiscover_tasks()


# ============ CELERY BEAT SCHEDULE ============

app.conf.beat_schedule = {

    # ── Check for scheduled streams every 5 minutes ──────────────────
    'start-scheduled-streams': {
        'task': 'apps.streaming.tasks.start_scheduled_streams',
        'schedule': crontab(minute='*/5'),
        'options': {'queue': 'celery'},
    },

    # ── Health-check all running streams every hour ───────────────────
    'check-stream-health-hourly': {
        'task': 'apps.streaming.tasks.check_stream_health',
        'schedule': crontab(minute=0),          # top of every hour
        'options': {'queue': 'celery'},
    },

    # ── Clean up orphaned YouTube broadcasts every hour ───────────────
    # Catches broadcasts left open when a stream worker crashes without
    # running stop_stream() / _end_youtube_broadcast().
    'cleanup-orphaned-broadcasts-hourly': {
        'task': 'apps.streaming.stream_manager.cleanup_orphaned_broadcasts',
        'schedule': crontab(minute=30),         # half-past every hour
        'options': {'queue': 'celery'},
    },

    # ── Check subscription expiry daily at midnight ───────────────────
    'check-subscription-expiry-daily': {
        'task': 'apps.payments.tasks.check_subscription_expiry',
        'schedule': crontab(hour=0, minute=0),
        'options': {'queue': 'celery'},
    },

    # ── Clean up old stream logs every Sunday at 2 AM ────────────────
    'cleanup-old-logs-weekly': {
        'task': 'apps.streaming.tasks.cleanup_old_logs',
        'schedule': crontab(hour=2, minute=0, day_of_week=0),
        'options': {'queue': 'celery'},
    },
}


# ============ WORKER CONFIGURATION ============

# Only acknowledge a task after it completes — prevents data loss if a
# worker crashes mid-stream.
app.conf.task_acks_late = True
app.conf.task_reject_on_worker_lost = True

# Pull one task at a time. Critical for the 'streaming' queue: a worker
# running a 24-hour stream task must not pick up a second stream task.
app.conf.worker_prefetch_multiplier = 1

# Recycle streaming workers after every task so sub-processes (FFmpeg,
# yt-dlp) don't accumulate in long-lived workers.
app.conf.worker_max_tasks_per_child = 1

# Keep result data for 6 hours (stream tasks produce minimal output)
app.conf.result_expires = 6 * 3600

# Enable task events so Flower / monitoring dashboards can track tasks
app.conf.worker_send_task_events = True
app.conf.task_send_sent_event = True

# High visibility timeout: Redis won't re-queue a task that's been
# "in progress" for up to 25 hours.  Matches the 24 h time_limit on
# stream_playlist_direct_async.
app.conf.broker_transport_options = {
    'visibility_timeout': 25 * 3600,   # 25 hours in seconds
}


# ============ TASK ROUTING ============
# 'streaming' queue  → long-running workers (1 task per worker)
# 'celery' queue     → short periodic tasks and utility tasks

app.conf.task_routes = {
    # Long-running stream control tasks
    'apps.streaming.tasks.stream_playlist_direct_async': {'queue': 'streaming'},
    'apps.streaming.tasks.start_stream_async':           {'queue': 'streaming'},
    'apps.streaming.tasks.stop_stream_async':            {'queue': 'streaming'},
    'apps.streaming.tasks.restart_stream_async':         {'queue': 'streaming'},
    'apps.streaming.tasks.download_playlist_videos_async': {'queue': 'streaming'},

    # Short periodic / utility tasks
    'apps.streaming.tasks.start_scheduled_streams':      {'queue': 'celery'},
    'apps.streaming.tasks.check_stream_health':          {'queue': 'celery'},
    'apps.streaming.tasks.cleanup_old_logs':             {'queue': 'celery'},
    'apps.streaming.stream_manager.cleanup_orphaned_broadcasts': {'queue': 'celery'},

    # Payment tasks
    'apps.payments.tasks.*': {'queue': 'celery'},
}
