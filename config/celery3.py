import os
import logging
from celery import Celery
from celery.schedules import crontab

logger = logging.getLogger(__name__)

# Set the default Django settings module
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

# Create Celery app
app = Celery('youtube_streamer')

# Load config from Django settings with CELERY_ prefix
app.config_from_object('django.conf:settings', namespace='CELERY')

# Auto-discover tasks from all registered Django apps
app.autodiscover_tasks()

# ============ CELERY BEAT SCHEDULE ============
# Periodic tasks that run automatically

app.conf.beat_schedule = {
    # Check for scheduled streams EVERY 5 MINUTES
    'start-scheduled-streams': {
        'task': 'apps.streaming.tasks.start_scheduled_streams',
        'schedule': crontab(minute='*/5'),  # Every 5 minutes
        'options': {'queue': 'streaming'},
    },
    
    # Check stream health EVERY HOUR (at :00)
    'check-stream-health-hourly': {
        'task': 'apps.streaming.tasks.check_stream_health',
        'schedule': crontab(minute=0),  # ✅ FIXED
        'options': {'queue': 'celery'},
    },
    
    # Check subscription expiry DAILY AT MIDNIGHT
    'check-subscription-expiry-daily': {
        'task': 'apps.payments.tasks.check_subscription_expiry',
        'schedule': crontab(hour=0, minute=0),  # Daily at midnight
        'options': {'queue': 'celery'},
    },
    
    # Clean up old logs WEEKLY (Sunday at 2 AM)
    'cleanup-old-logs-weekly': {
        'task': 'apps.streaming.tasks.cleanup_old_logs',
        'schedule': crontab(hour=2, minute=0, day_of_week=0),  # Sunday 2 AM
        'options': {'queue': 'celery'},
    },
}  # ✅ PROPERLY CLOSED

# ============ CELERY CONFIGURATIONS ============

# Task routing (optional - helpful for scaling)
app.conf.task_routes = {
    'apps.streaming.tasks.*': {'queue': 'streaming'},
    'apps.payments.tasks.*': {'queue': 'celery'},
}

# Task settings
app.conf.task_acks_late = True
app.conf.task_reject_on_worker_lost = True
app.conf.worker_prefetch_multiplier = 1
app.conf.worker_max_tasks_per_child = 1000

# Results expire after 1 hour (save memory)
app.conf.result_expires = 3600

# Enable task events (for monitoring)
app.conf.worker_send_task_events = True


@app.task(bind=True)
def debug_task(self):
    """Debug task - remove in production"""
    print(f'Request: {self.request!r}')
