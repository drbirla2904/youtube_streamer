# Stream Scheduling Fix Summary

## Problem
Error: "stream scheduling on youtube not starting stream"

Scheduled streams were not automatically starting because:
1. No 'scheduled' status was defined in the model
2. No field to store when streams should start
3. No Celery task to check and start scheduled streams
4. YouTube auto-start was disabled (`enableAutoStart: False`)

## Root Causes

### 1. **Incomplete Model Definition**
- Views referenced `status='scheduled'` but model only had: idle, starting, running, stopping, stopped, error
- Model CheckConstraint didn't include 'scheduled' status (would cause validation errors)
- No `scheduled_start_time` field to store when streams should start

### 2. **No Automation Task**
- Celery Beat schedule had no task to check for and start scheduled streams
- Manual streaming worked, but automatic scheduling had no trigger

### 3. **Wrong YouTube Settings**
- `enableAutoStart` was set to `False` instead of `True`
- YouTube wouldn't automatically start the broadcast

## Fixes Applied

### 1. **Updated Stream Model** (`models.py`)

**Added 'scheduled' to STATUS_CHOICES:**
```python
STATUS_CHOICES = [
    ('idle', 'Idle'),
    ('scheduled', 'Scheduled'),      # ✅ NEW
    ('starting', 'Starting'),
    ('running', 'Running'),
    ('stopping', 'Stopping'),
    ('stopped', 'Stopped'),
    ('error', 'Error'),
]
```

**Added scheduled_start_time field:**
```python
scheduled_start_time = models.DateTimeField(
    null=True, 
    blank=True, 
    db_index=True,
    help_text='When the stream should automatically start'
)
```

**Updated CheckConstraint:**
```python
models.CheckConstraint(
    check=models.Q(
        status__in=['idle', 'scheduled', 'starting', 'running', 'stopping', 'stopped', 'error']
    ),
    name='valid_stream_status'
),
```

### 2. **Created Scheduled Stream Task** (`tasks.py`)

New `start_scheduled_streams()` task:
```python
@shared_task
def start_scheduled_streams():
    """Check for scheduled streams that should start NOW"""
    now = timezone.now()
    
    # Find all streams scheduled to start within the last 5 minutes
    scheduled_streams = Stream.objects.filter(
        status='scheduled',
        scheduled_start_time__lte=now,
        is_deleted=False
    ).select_for_update()
    
    for stream in scheduled_streams:
        # Create YouTube broadcast
        # Start FFmpeg streaming
        # Update status to running
```

### 3. **Added Task to Celery Beat** (`config/celery.py`)

Registered task to run **every 5 minutes**:
```python
'start-scheduled-streams': {
    'task': 'apps.streaming.tasks.start_scheduled_streams',
    'schedule': crontab(minute='*/5'),  # Every 5 minutes
    'options': {'queue': 'streaming'},
},
```

### 4. **Enabled YouTube Auto-Start** (`stream_manager.py`)

Fixed `enableAutoStart`:
```python
'contentDetails': {
    'enableAutoStart': True,   # ✅ Auto-start when stream ready
    'enableAutoStop': True,
    'enableDvr': True,
    'recordFromStart': True,
}
```

### 5. **Updated Stream Creation View** (`views.py`)

Now handles scheduled start time:
```python
if request.method == 'POST':
    scheduled_start_time_str = request.POST.get('scheduled_start_time')
    
    if scheduled_start_time_str:
        scheduled_start_time = datetime.fromisoformat(scheduled_start_time_str)
        stream_status = 'scheduled'
    
    stream = Stream.objects.create(
        ...
        status=stream_status,
        scheduled_start_time=scheduled_start_time
    )
```

### 6. **Created Migration** (`migrations/0002_stream_scheduling.py`)

Database migration to add the new field and update constraints

## How It Works

1. **User schedules stream**: Creates stream with `status='scheduled'` and `scheduled_start_time`
2. **Celery Beat triggers every 5 minutes**: `start_scheduled_streams()` task runs
3. **Task checks for due streams**: Finds any with `scheduled_start_time <= now()`
4. **Task starts streams automatically**:
   - Creates YouTube broadcast (with `enableAutoStart=True`)
   - Starts FFmpeg streaming
   - Updates status to 'running'
   - Logs the event

## Usage

### To schedule a stream (in frontend):
```html
<form method="POST">
    <input type="text" name="title" required>
    <input type="datetime-local" name="scheduled_start_time" required>
    <select name="youtube_account" required></select>
    <select name="playlist_id" required></select>
    <button type="submit">Schedule Stream</button>
</form>
```

### To check scheduled streams (Django shell):
```python
from apps.streaming.models import Stream
from django.utils import timezone

Stream.objects.filter(
    status='scheduled',
    scheduled_start_time__lte=timezone.now()
)
```

## Files Modified

1. `/workspaces/youtube_streamer/apps/streaming/models.py`
   - Added 'scheduled' to STATUS_CHOICES
   - Added `scheduled_start_time` field
   - Updated CheckConstraint

2. `/workspaces/youtube_streamer/apps/streaming/tasks.py`
   - Added `start_scheduled_streams()` task

3. `/workspaces/youtube_streamer/config/celery.py`
   - Added task to beat_schedule with 5-minute interval

4. `/workspaces/youtube_streamer/apps/streaming/stream_manager.py`
   - Changed `enableAutoStart` from False to True

5. `/workspaces/youtube_streamer/apps/streaming/views.py`
   - Added scheduled_start_time handling in stream_create

6. `/workspaces/youtube_streamer/apps/streaming/migrations/0002_stream_scheduling.py`
   - NEW: Database migration

## Next Steps

1. **Run migration:**
   ```bash
   python manage.py migrate streaming
   ```

2. **Restart Celery Beat:**
   ```bash
   celery -A config beat -l info
   ```

3. **Restart Celery Worker:**
   ```bash
   celery -A config worker -l info -Q streaming,celery
   ```

4. **Test by creating a scheduled stream** with a start time 2-3 minutes in the future

## Troubleshooting

### Scheduled streams not starting:
1. Check Celery Beat is running: `celery -A config inspect active_queues`
2. Check task in queue: `celery -A config inspect reserved`
3. Monitor task execution: `tail -f logs/celery.log`

### Verify task is registered:
```bash
python manage.py shell
> from apps.streaming.tasks import start_scheduled_streams
> start_scheduled_streams.delay()
```

### Check stream status:
```python
stream = Stream.objects.get(id='...')
print(f"Status: {stream.status}")
print(f"Scheduled for: {stream.scheduled_start_time}")
```
