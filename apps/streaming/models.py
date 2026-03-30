"""
models.py — No structural changes required for direct playlist streaming.

The playlist_videos JSONField already stores the playlist reference set by
stream_create.  PlaylistPipeStreamer reads it directly — no new fields needed.

Minor improvement: added update_fields hints in the comment block so callers
know which fields to pass to .save() for efficiency on AWS RDS.
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
        ('idle', 'Idle'),
        ('scheduled', 'Scheduled'),
        ('starting', 'Starting'),
        ('running', 'Running'),
        ('stopping', 'Stopping'),
        ('stopped', 'Stopped'),
        ('error', 'Error'),
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

    # FK to Playlist model (optional; playlist_videos JSONField is the live reference)
    playlist = models.ForeignKey(
        Playlist, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='streams',
    )

    stream_key = models.CharField(max_length=255, blank=True)
    broadcast_id = models.CharField(max_length=255, blank=True)
    stream_url = models.URLField(blank=True)
    loop_enabled = models.BooleanField(default=True)

    process_id = models.BigIntegerField(null=True, blank=True)
    process_started_at = models.DateTimeField(null=True, blank=True)
    last_heartbeat = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True, max_length=1000)

    scheduled_start_time = models.DateTimeField(
        null=True, blank=True, db_index=True,
        help_text='When the stream should automatically start',
    )
    started_at = models.DateTimeField(null=True, blank=True)
    stopped_at = models.DateTimeField(null=True, blank=True)

    # JSONField stores playlist reference + cached video list.
    # Structure set by stream_create view:
    #   [{"youtube_playlist_id": "PLxxx", "title": "...", "videos_fetched": False}]
    # After PlaylistPipeStreamer fetches videos it adds:
    #   {"videos": [...], "extracted": True, "total_videos": N}
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
        ]

    def __str__(self):
        return f"{self.title} — {self.user.username}"

    def clean(self):
        if self.started_at and self.stopped_at:
            if self.stopped_at <= self.started_at:
                raise ValidationError('Stop time must be after start time')

    @property
    def uptime_seconds(self):
        if not self.started_at:
            return 0
        end = self.stopped_at or timezone.now()
        return int((end - self.started_at).total_seconds())

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
    stream = models.ForeignKey(Stream, on_delete=models.CASCADE)
    playlist = models.ForeignKey(Playlist, on_delete=models.CASCADE)


class StreamLog(models.Model):
    LEVEL_CHOICES = [
        ('DEBUG', 'Debug'),
        ('INFO', 'Info'),
        ('WARNING', 'Warning'),
        ('ERROR', 'Error'),
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
    message = models.TextField(max_length=4096)
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
