"""
models.py — Scheduling (once / daily) + auto_upload_after_end support.

New fields on Stream:
  schedule_type          — 'now' | 'once' | 'daily'
  scheduled_end_time     — datetime, for 'once' streams with optional end
  daily_start_time       — 'HH:MM' string for daily recurring start
  daily_end_time         — 'HH:MM' string for daily recurring end (optional)
  auto_upload_after_end  — bool; controls recordFromStart in create_broadcast()

Run:
  python manage.py makemigrations
  python manage.py migrate
"""

from django.db import models
from django.db.models import JSONField
from django.contrib.auth.models import User
from django.core.validators import MinValueValidator, MaxValueValidator, MinLengthValidator
from django.core.exceptions import ValidationError
from django.utils import timezone
from apps.accounts.models import YouTubeAccount
import uuid
from datetime import timedelta


class SoftDeleteManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)


class BaseModel(models.Model):
    """Abstract base with soft-delete support."""
    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SoftDeleteManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True


class MediaFile(BaseModel):
    MEDIA_TYPES = [('video', 'Video'), ('audio', 'Audio')]

    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        User, on_delete=models.CASCADE,
        related_name='media_files', db_index=True,
    )
    title = models.CharField(max_length=255, validators=[MinLengthValidator(1)])
    file = models.FileField(upload_to='uploads/media/')
    thumbnail = models.ImageField(
        upload_to='uploads/thumbnails/', blank=True, null=True
    )
    media_type = models.CharField(max_length=10, choices=MEDIA_TYPES, db_index=True)
    mime_type = models.CharField(max_length=50, blank=True)
    sequence = models.PositiveIntegerField(default=0, db_index=True)
    duration = models.FloatField(
        default=0.0,
        validators=[MinValueValidator(0.0), MaxValueValidator(86400.0)],
    )
    file_size = models.BigIntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(5 * 1024 ** 3)],
    )

    class Meta:
        verbose_name = 'Media File'
        verbose_name_plural = 'Media Files'
        indexes = [
            models.Index(fields=['user', 'sequence']),
            models.Index(fields=['user', 'created_at']),
            models.Index(fields=['user', 'is_deleted']),
        ]

    def __str__(self):
        return self.title


class Playlist(models.Model):
    """Cached YouTube playlist metadata."""
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    playlist_id = models.CharField(max_length=255, unique=True)
    channel_id = models.CharField(max_length=255)
    title = models.CharField(max_length=255)
    video_count = models.IntegerField(default=0)
    last_fetched = models.DateTimeField(auto_now=True)
    total_duration = models.DurationField(null=True, blank=True)

    def __str__(self):
        return f"{self.title} ({self.video_count} videos)"


class Stream(BaseModel):
    STATUS_CHOICES = [
        ('idle',      'Idle'),
        ('scheduled', 'Scheduled'),
        ('starting',  'Starting'),
        ('running',   'Running'),
        ('stopping',  'Stopping'),
        ('stopped',   'Stopped'),
        ('error',     'Error'),
    ]

    SCHEDULE_TYPE_CHOICES = [
        ('now',   'Live Now'),
        ('once',  'Schedule Once'),
        ('daily', 'Daily Recurring'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User, on_delete=models.CASCADE,
        related_name='streams', db_index=True,
    )
    youtube_account = models.ForeignKey(
        YouTubeAccount, on_delete=models.CASCADE,
        related_name='streams', db_index=True,
    )
    title = models.CharField(max_length=255, validators=[MinLengthValidator(1)])
    description = models.TextField(blank=True, max_length=5000)
    thumbnail = models.ImageField(
        upload_to='uploads/stream_thumbnails/', blank=True, null=True
    )
    media_files = models.ManyToManyField(MediaFile, related_name='streams', blank=True)

    playlist = models.ForeignKey(
        Playlist, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='streams',
    )

    stream_key  = models.CharField(max_length=255, blank=True)
    broadcast_id = models.CharField(max_length=255, blank=True)
    stream_url  = models.URLField(blank=True)
    loop_enabled = models.BooleanField(default=True)

    process_id          = models.BigIntegerField(null=True, blank=True)
    process_started_at  = models.DateTimeField(null=True, blank=True)
    last_heartbeat      = models.DateTimeField(null=True, blank=True)
    error_message       = models.TextField(blank=True, max_length=1000)

    # ── Scheduling ────────────────────────────────────────────────────────────
    schedule_type = models.CharField(
        max_length=10,
        choices=SCHEDULE_TYPE_CHOICES,
        default='now',
        db_index=True,
    )

    # 'once' — one-time scheduled start (existing field, kept for compatibility)
    scheduled_start_time = models.DateTimeField(
        null=True, blank=True, db_index=True,
        help_text='One-time: when the stream should automatically start',
    )
    # 'once' — optional end time
    scheduled_end_time = models.DateTimeField(
        null=True, blank=True,
        help_text='One-time: when the stream should automatically stop',
    )

    # 'daily' — stored as "HH:MM" strings (timezone-naive, interpreted as server local)
    daily_start_time = models.CharField(
        max_length=5, blank=True,
        help_text='Daily: start time in HH:MM format (e.g. 09:00)',
    )
    daily_end_time = models.CharField(
        max_length=5, blank=True,
        help_text='Daily: end time in HH:MM format (optional)',
    )

    # ── Auto upload ───────────────────────────────────────────────────────────
    auto_upload_after_end = models.BooleanField(
        default=False,
        help_text='If True, YouTube will record the broadcast and save it as a video automatically (sets recordFromStart=True on the broadcast)',
    )

    # ── Runtime timestamps ────────────────────────────────────────────────────
    started_at  = models.DateTimeField(null=True, blank=True)
    stopped_at  = models.DateTimeField(null=True, blank=True)

    playlist_videos = JSONField(default=list)

    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES,
        default='idle', db_index=True,
    )

    class Meta:
        verbose_name = 'Stream'
        verbose_name_plural = 'Streams'
        constraints = [
            models.UniqueConstraint(
                fields=['youtube_account', 'status'],
                condition=models.Q(
                    status__in=['running', 'starting'], is_deleted=False
                ),
                name='unique_active_stream_per_channel',
            ),
            models.CheckConstraint(
                check=models.Q(
                    status__in=[
                        'idle', 'scheduled', 'starting', 'running',
                        'stopping', 'stopped', 'error',
                    ]
                ),
                name='valid_stream_status',
            ),
        ]
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['user', 'created_at']),
            models.Index(fields=['youtube_account', 'status']),
            models.Index(fields=['is_deleted', 'user']),
            models.Index(fields=['schedule_type', 'status']),
            models.Index(fields=['daily_start_time']),
        ]

    def __str__(self):
        return f"{self.title} — {self.user.username}"

    def clean(self):
        if self.started_at and self.stopped_at:
            if self.stopped_at <= self.started_at:
                raise ValidationError('Stop time must be after start time')
        if self.schedule_type == 'once' and self.scheduled_start_time and self.scheduled_end_time:
            if self.scheduled_end_time <= self.scheduled_start_time:
                raise ValidationError('End time must be after start time')

    @property
    def uptime_seconds(self):
        if not self.started_at:
            return 0
        end = self.stopped_at or timezone.now()
        return int((end - self.started_at).total_seconds())

    @property
    def schedule_display(self):
        """Human-readable schedule summary for templates."""
        if self.schedule_type == 'now':
            return 'Live Now'
        if self.schedule_type == 'once':
            s = self.scheduled_start_time
            e = self.scheduled_end_time
            if s and e:
                return f"Once · {s.strftime('%b %d, %Y %H:%M')} → {e.strftime('%H:%M')}"
            if s:
                return f"Once · {s.strftime('%b %d, %Y %H:%M')}"
            return 'Once (no time set)'
        if self.schedule_type == 'daily':
            if self.daily_start_time and self.daily_end_time:
                return f"Daily · {self.daily_start_time} → {self.daily_end_time}"
            if self.daily_start_time:
                return f"Daily · {self.daily_start_time}"
            return 'Daily (no time set)'
        return self.schedule_type

    def is_process_alive(self) -> bool:
        import os
        if not self.process_id:
            return False
        if self.last_heartbeat:
            if timezone.now() - self.last_heartbeat > timedelta(minutes=2):
                return False
        try:
            os.kill(self.process_id, 0)
            return True
        except (ProcessLookupError, OSError):
            return False


class StreamPlaylist(models.Model):
    stream   = models.ForeignKey(Stream,   on_delete=models.CASCADE)
    playlist = models.ForeignKey(Playlist, on_delete=models.CASCADE)


class StreamLog(models.Model):
    LEVEL_CHOICES = [
        ('DEBUG',    'Debug'),
        ('INFO',     'Info'),
        ('WARNING',  'Warning'),
        ('ERROR',    'Error'),
        ('CRITICAL', 'Critical'),
    ]

    stream = models.ForeignKey(
        Stream, on_delete=models.CASCADE,
        related_name='logs', db_index=True,
    )
    level = models.CharField(
        max_length=10, choices=LEVEL_CHOICES,
        default='INFO', db_index=True,
    )
    message    = models.TextField(max_length=4096)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = 'Stream Log'
        verbose_name_plural = 'Stream Logs'
        indexes = [
            models.Index(fields=['stream', 'level', 'created_at']),
        ]

    def __str__(self):
        return f"{self.stream.title} — {self.level}"

    @classmethod
    def cleanup_old_logs(cls, days=90):
        cutoff = timezone.now() - timedelta(days=days)
        return cls.objects.filter(created_at__lt=cutoff).delete()
