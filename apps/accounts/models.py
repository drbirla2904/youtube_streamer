from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from django.conf import settings
import logging
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
import json

logger = logging.getLogger(__name__)


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    phone = models.CharField(max_length=15, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username}'s Profile"

    class Meta:
        verbose_name = 'User Profile'
        verbose_name_plural = 'User Profiles'


class YouTubeAccount(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='youtube_accounts')
    channel_id = models.CharField(max_length=255, unique=True)
    channel_title = models.CharField(max_length=255)
    access_token = models.TextField()
    refresh_token = models.TextField()
    token_expiry = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Netscape-format cookies.txt for yt-dlp authentication.
    # YouTube's bot detection blocks server-side requests that lack real
    # browser session cookies. The OAuth access_token alone is insufficient
    # because yt-dlp's download requests go through a different path than
    # the YouTube Data API. Real browser cookies (exported via a browser
    # extension) are the only reliable bypass.
    cookies_txt = models.TextField(
        blank=True,
        default='',
        help_text=(
            'Paste Netscape-format cookies exported from your browser. '
            'Use "Get cookies.txt LOCALLY" (Chrome) or "cookies.txt" (Firefox). '
            'Visit youtube.com while signed in, then export cookies for youtube.com.'
        ),
    )

    def __str__(self):
        return f"{self.channel_title} - {self.user.username}"

    def is_token_expired(self):
        if not self.token_expiry:
            return True
        return timezone.now() >= self.token_expiry

    def has_cookies(self) -> bool:
        """Return True if a non-empty cookies_txt has been uploaded."""
        return bool(self.cookies_txt and self.cookies_txt.strip())

    def get_credentials(self):
        """Reconstruct OAuth credentials from stored tokens."""
        if not self.access_token:
            return None

        creds = Credentials.from_authorized_user_info({
            'token': self.access_token,
            'refresh_token': self.refresh_token,
            'token_uri': 'https://oauth2.googleapis.com/token',
            'client_id': settings.GOOGLE_CLIENT_ID,
            'client_secret': settings.GOOGLE_CLIENT_SECRET,
        })

        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self.access_token = creds.token
                self.save(update_fields=['access_token'])
            except Exception as e:
                logger.error(f"Failed to refresh YouTube credentials: {e}")

        return creds

    class Meta:
        verbose_name = 'YouTube Account'
        verbose_name_plural = 'YouTube Accounts'