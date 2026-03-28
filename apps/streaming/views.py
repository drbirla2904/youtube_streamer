from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_protect
from django.db import transaction, IntegrityError
from django_ratelimit.decorators import ratelimit
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from django.conf import settings
from django.core.cache import cache
from datetime import datetime, timedelta
import os
import json
import subprocess
import magic
import logging
from typing import Tuple
from googleapiclient.errors import HttpError
from .models import Stream, MediaFile, StreamLog
from apps.accounts.models import YouTubeAccount
from apps.payments.models import Subscription
from .stream_manager import StreamManager, get_temp_dir_for_stream
from google.auth.transport.requests import Request

logger = logging.getLogger(__name__)

# ============ HELPERS ============

def get_user_storage_usage(user) -> int:
    return sum(
        media.file_size for media in MediaFile.objects.filter(user=user)
        if media.file_size
    ) or 0


def has_storage_available(user, file_size: int) -> Tuple[bool, int, int]:
    subscription = Subscription.objects.filter(
        user=user, is_active=True, status='active'
    ).first()
    if not subscription:
        return False, 0, 0
    current_usage = get_user_storage_usage(user)
    available_storage = subscription.storage_limit - current_usage
    if file_size > available_storage:
        return False, current_usage, subscription.storage_limit
    return True, current_usage, subscription.storage_limit


def format_bytes(bytes_size: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024:
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.2f} TB"


def validate_file_upload(uploaded_file):
    ALLOWED_TYPES = {
        'video/mp4': ['mp4'],
        'video/quicktime': ['mov'],
        'audio/mpeg': ['mp3'],
        'audio/wav': ['wav'],
    }
    try:
        mime = magic.Magic(mime=True)
        file_mime = mime.from_buffer(uploaded_file.read(4096))
        uploaded_file.seek(0)
        if file_mime not in ALLOWED_TYPES:
            raise Exception(f'Invalid file type: {file_mime}')
        ext = uploaded_file.name.split('.')[-1].lower()
        if ext not in ALLOWED_TYPES[file_mime]:
            raise Exception(f'Extension .{ext} does not match {file_mime}')
        if uploaded_file.size > 5 * 1024**3:
            raise Exception('File too large (max 5GB)')
        if uploaded_file.size < 1024:
            raise Exception('File too small (min 1KB)')
        return True
    except Exception as e:
        raise Exception(f'File validation failed: {str(e)}')


# ============ VIEWS ============

@login_required
@require_POST
@csrf_protect
def media_reorder_view(request):
    try:
        data = json.loads(request.body)
        order = data.get('order', [])
        for item in order:
            MediaFile.objects.filter(
                id=item['id'], user=request.user
            ).update(sequence=item['sequence'])
        return JsonResponse({'status': 'success'})
    except Exception as e:
        logger.error(f"Reorder failed: {e}")
        return JsonResponse({'status': 'error'}, status=400)


@login_required
def connect_youtube(request):
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [settings.GOOGLE_REDIRECT_URI],
            }
        },
        scopes=settings.GOOGLE_SCOPES,
        redirect_uri=settings.GOOGLE_REDIRECT_URI
    )
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent'
    )
    request.session['oauth_state'] = state
    return redirect(authorization_url)


@login_required
def oauth_callback(request):
    try:
        url_state = request.GET.get('state')
        session_state = request.session.get('oauth_state')
        if not url_state or url_state != session_state:
            logger.warning(f"OAuth CSRF detected for user {request.user.id}")
            messages.error(request, 'OAuth validation failed')
            return redirect('dashboard')

        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": settings.GOOGLE_CLIENT_ID,
                    "client_secret": settings.GOOGLE_CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [settings.GOOGLE_REDIRECT_URI],
                }
            },
            scopes=settings.GOOGLE_SCOPES,
            state=url_state,
            redirect_uri=settings.GOOGLE_REDIRECT_URI
        )
        flow.fetch_token(authorization_response=request.build_absolute_uri())
        credentials = flow.credentials

        youtube = build('youtube', 'v3', credentials=credentials)
        channel_response = youtube.channels().list(
            part='snippet,contentDetails', mine=True
        ).execute()

        if channel_response['items']:
            channel = channel_response['items'][0]
            channel_id = channel['id']
            channel_title = channel['snippet']['title']
            YouTubeAccount.objects.update_or_create(
                user=request.user,
                channel_id=channel_id,
                defaults={
                    'channel_title': channel_title,
                    'access_token': credentials.token,
                    'refresh_token': credentials.refresh_token,
                    'token_expiry': credentials.expiry,
                    'is_active': True
                }
            )
            logger.info(f'YouTube connected: {channel_id} user={request.user.id}')
            messages.success(request, f'Connected: {channel_title}')
        else:
            messages.error(request, 'No YouTube channel found')

    except Exception as e:
        logger.error(f"OAuth callback failed: {e}")
        messages.error(request, 'Connection failed')
    finally:
        request.session.pop('oauth_state', None)

    return redirect('dashboard')


@login_required
def stream_list(request):
    streams = Stream.objects.filter(
        user=request.user
    ).select_related('youtube_account').order_by('-created_at')
    return render(request, 'streaming/stream_list.html', {'streams': streams})


@login_required
@transaction.atomic
def stream_create(request):
    subscription = Subscription.objects.filter(
        user=request.user, is_active=True
    ).select_for_update().first()

    if not subscription:
        messages.error(request, 'You need an active subscription')
        return redirect('subscribe')

    active_streams = Stream.objects.filter(
        user=request.user,
        status__in=['running', 'starting', 'scheduled']
    ).count()

    if active_streams >= subscription.max_streams:
        messages.error(request, f'Stream limit reached ({subscription.max_streams})')
        return redirect('stream_list')

    youtube_accounts = YouTubeAccount.objects.filter(user=request.user, is_active=True)
    if not youtube_accounts.exists():
        messages.error(request, 'Connect YouTube first')
        return redirect('connect_youtube')

    playlists = []
    account = youtube_accounts.first()
    if account:
        try:
            youtube = build('youtube', 'v3', credentials=account.get_credentials())
            response = youtube.playlists().list(
                part='snippet,contentDetails',
                mine=True,
                maxResults=50
            ).execute()
            playlists = [{
                'id': p['id'],
                'title': p['snippet']['title'],
                'video_count': int(p['contentDetails']['itemCount']),
                'thumbnail': p['snippet']['thumbnails']['medium']['url']
            } for p in response.get('items', [])]
        except Exception as e:
            logger.error(f"Playlist fetch error: {e}")

    if request.method == 'POST':
        try:
            title = request.POST.get('title', '').strip()
            description = request.POST.get('description', '')
            youtube_account_id = request.POST.get('youtube_account')
            playlist_id = request.POST.get('playlist_id', '').strip()
            scheduled_start_time_str = request.POST.get('scheduled_start_time', '').strip()

            youtube_account = YouTubeAccount.objects.get(
                id=youtube_account_id, user=request.user
            )

            scheduled_start_time = None
            stream_status = 'idle'

            if scheduled_start_time_str:
                try:
                    scheduled_start_time = datetime.fromisoformat(scheduled_start_time_str)
                    if scheduled_start_time.tzinfo is None:
                        from django.utils import timezone
                        scheduled_start_time = timezone.make_aware(scheduled_start_time)
                    stream_status = 'scheduled'
                except Exception as e:
                    logger.error(f"Failed to parse scheduled time: {e}")
                    messages.warning(request, "Invalid scheduled time, creating in idle state")

            stream = Stream.objects.create(
                user=request.user,
                youtube_account=youtube_account,
                title=title,
                description=description,
                playlist_videos=[{
                    "youtube_playlist_id": playlist_id,
                    "title": f"YouTube Playlist: {playlist_id}",
                    "videos_fetched": False
                }],
                thumbnail=request.FILES.get('thumbnail'),
                status=stream_status,
                scheduled_start_time=scheduled_start_time
            )

            if stream_status == 'scheduled':
                messages.success(request, f'Stream "{title}" scheduled!')
            else:
                messages.success(request, f'Stream "{title}" created!')
            return redirect('stream_detail', stream_id=stream.id)

        except Exception as e:
            logger.error(f"Stream creation failed: {e}")
            messages.error(request, f'Error: {str(e)}')

    return render(request, 'streaming/stream_create.html', {
        'youtube_accounts': youtube_accounts,
        'playlists': playlists,
    })


@login_required
def stream_detail(request, stream_id):
    stream = get_object_or_404(Stream, id=stream_id, user=request.user)
    logs = stream.logs.all()[:50]
    return render(request, 'streaming/stream_detail.html', {
        'stream': stream,
        'logs': logs,
    })


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# THE FIX:
#   Original code had:
#       @ratelimit(key='user', rate='5/h', method='POST')   â† only blocked POST
#       @login_required                                       â† duplicate decorator
#   The template uses <a href="{% url 'stream_start' %}">    â† sends a GET request
#
#   Django's @ratelimit with method='POST' does NOT block GET, but the
#   interaction with @login_required ordering caused a 403 on some Django
#   versions.  More importantly the template was never using a form/POST,
#   so the view needs to accept GET.
#
#   Fix: remove @require_POST, remove duplicate @login_required, set
#   ratelimit to method='ALL' so it counts every visit regardless of method.
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@login_required
@ratelimit(key='user', rate='5/h', method='ALL', block=True)
def stream_start(request, stream_id):
    """Start a stream (called via plain GET link from stream_detail template)"""
    stream = get_object_or_404(Stream, id=stream_id, user=request.user)

    if stream.status in ('running', 'starting'):
        messages.warning(request, 'Stream is already running or starting')
        return redirect('stream_detail', stream_id=stream.id)

    try:
        stream.status = 'starting'
        stream.save()

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
            message='Stream started successfully'
        )
        messages.success(request, 'Stream started successfully!')

    except Exception as e:
        stream.status = 'error'
        stream.error_message = str(e)
        stream.save()
        StreamLog.objects.create(
            stream=stream,
            level='ERROR',
            message=f'Failed to start stream: {str(e)}'
        )
        messages.error(request, f'Failed to start stream: {str(e)}')

    return redirect('stream_detail', stream_id=stream.id)


@login_required
@ratelimit(key='user', rate='5/h', method='ALL', block=True)
def stream_stop(request, stream_id):
    """Gracefully stop a running stream (called via plain GET link)"""
    stream = get_object_or_404(Stream, id=stream_id, user=request.user)

    try:
        stream.status = 'stopping'
        stream.save()

        manager = StreamManager(stream)
        success = manager.stop_stream()

        if success:
            StreamLog.objects.create(
                stream=stream,
                level='INFO',
                message='Stream stopped gracefully'
            )
            messages.success(request, 'Stream stopped')
        else:
            StreamLog.objects.create(
                stream=stream,
                level='WARNING',
                message='Stream stop encountered errors'
            )
            messages.warning(request, 'Stop command sent (check logs)')

    except Exception as e:
        logger.error(f"Stop error: {e}", exc_info=True)
        messages.error(request, 'Error stopping stream')

    return redirect('stream_detail', stream_id=stream.id)


@login_required
def stream_delete(request, stream_id):
    stream = get_object_or_404(Stream, id=stream_id, user=request.user)

    if stream.status in ['running', 'starting']:
        messages.error(request, 'Stop stream first')
        return redirect('stream_detail', stream_id=stream.id)

    try:
        manager = StreamManager(stream)
        manager.stop_stream()
        stream.delete()
        messages.success(request, 'Stream deleted')
    except Exception as e:
        logger.error(f"Delete error: {e}")
        messages.error(request, 'Delete failed')

    return redirect('stream_list')


@login_required
@require_POST
def download_playlist_videos_view(request, stream_id):
    stream = get_object_or_404(Stream, id=stream_id, user=request.user)

    if stream.status == 'running':
        messages.error(request, 'Stop stream first before downloading')
        return redirect('stream_detail', stream_id=stream.id)

    if not stream.playlist_videos:
        messages.error(request, 'Stream has no playlist data')
        return redirect('stream_detail', stream_id=stream.id)

    if stream.media_files.exists():
        messages.warning(request, 'Videos already downloaded')
        return redirect('stream_detail', stream_id=stream.id)

    try:
        from .tasks import download_playlist_videos_async
        max_videos = int(request.POST.get('max_videos', 50))
        task = download_playlist_videos_async.delay(str(stream.id), max_videos)
        messages.success(request, f'Download started (Task ID: {task.id})')
        StreamLog.objects.create(
            stream=stream,
            level='INFO',
            message=f'Started downloading playlist videos (max {max_videos})'
        )
        logger.info(f"Download task started for stream {stream.id}: {task.id}")
    except Exception as e:
        logger.error(f"Failed to start download: {e}")
        messages.error(request, f'Download failed: {str(e)}')

    return redirect('stream_detail', stream_id=stream.id)


@login_required
@transaction.atomic
def media_upload_view(request):
    subscription = Subscription.objects.filter(
        user=request.user, is_active=True, status='active'
    ).select_for_update().first()

    if not subscription:
        messages.error(request, 'No active subscription')
        return redirect('subscribe')

    if request.method == 'POST':
        try:
            file = request.FILES.get('file')
            title = request.POST.get('title', '').strip()
            validate_file_upload(file)
            has_storage, current, limit = has_storage_available(request.user, file.size)
            if not has_storage:
                messages.error(
                    request,
                    f'Storage full: {format_bytes(current)} / {format_bytes(limit)}'
                )
                return redirect('media_upload')
            MediaFile.objects.create(
                user=request.user,
                title=title,
                file=file,
                file_size=file.size,
                media_type='video'
            )
            messages.success(request, f'Uploaded ({format_bytes(file.size)})')
            return redirect('media_list')
        except Exception as e:
            logger.error(f"Upload failed: {e}")
            messages.error(request, f'Upload failed: {str(e)}')

    current = get_user_storage_usage(request.user)
    available = subscription.storage_limit - current
    return render(request, 'streaming/media_upload.html', {
        'storage_usage': format_bytes(current),
        'storage_limit': format_bytes(subscription.storage_limit),
        'storage_available': format_bytes(available),
        'storage_percent': (current / subscription.storage_limit) * 100,
    })


@login_required
def media_list_view(request):
    media_files = MediaFile.objects.filter(user=request.user).order_by('-created_at')
    subscription = Subscription.objects.filter(user=request.user, is_active=True).first()
    current = get_user_storage_usage(request.user)
    available = (subscription.storage_limit - current) if subscription else 0
    return render(request, 'streaming/media_list.html', {
        'media_files': media_files,
        'storage_usage': format_bytes(current),
        'storage_limit': format_bytes(subscription.storage_limit) if subscription else 'N/A',
        'storage_available': format_bytes(available),
    })


@login_required
@require_POST
def media_delete_view(request, media_id):
    media = get_object_or_404(MediaFile, id=media_id, user=request.user)
    media.file.delete(save=False)
    if media.thumbnail:
        media.thumbnail.delete(save=False)
    media.delete()
    subscription = Subscription.objects.filter(user=request.user, is_active=True).first()
    if subscription:
        current = get_user_storage_usage(request.user)
        messages.success(
            request,
            f'Deleted. Storage: {format_bytes(current)} / {format_bytes(subscription.storage_limit)}'
        )
    else:
        messages.success(request, 'Media deleted')
    return redirect('media_list')


@login_required
def stream_status_api(request, stream_id):
    stream = get_object_or_404(Stream, id=stream_id, user=request.user)
    return JsonResponse({
        'status': stream.status,
        'started_at': stream.started_at.isoformat() if stream.started_at else None,
        'uptime_seconds': stream.uptime_seconds,
        'error_message': stream.error_message,
        'is_process_alive': stream.is_process_alive(),
    })


@login_required
def user_playlists_api(request):
    account = YouTubeAccount.objects.filter(user=request.user, is_active=True).first()
    if not account:
        return JsonResponse({'playlists': []})
    try:
        youtube = build('youtube', 'v3', credentials=account.get_credentials())
        playlists = youtube.playlists().list(
            part='snippet,contentDetails',
            mine=True,
            maxResults=50
        ).execute()
        playlist_data = [{
            'id': p['id'],
            'title': p['snippet']['title'],
            'video_count': int(p['contentDetails']['itemCount']),
            'thumbnail': p['snippet']['thumbnails']['medium']['url']
        } for p in playlists.get('items', [])]
        return JsonResponse({'playlists': playlist_data})
    except Exception as e:
        logger.error(f"Playlist fetch error: {e}")
        return JsonResponse({'playlists': []})


@login_required
def playlist_videos_api(request, playlist_id):
    account = YouTubeAccount.objects.filter(user=request.user, is_active=True).first()
    if not account:
        return JsonResponse({'error': 'No YouTube account'}, status=400)
    try:
        youtube = build('youtube', 'v3', credentials=account.get_credentials())
        videos_response = youtube.playlistItems().list(
            part='snippet,contentDetails',
            playlistId=playlist_id,
            maxResults=50
        ).execute()
        videos = [{
            'video_id': item['contentDetails']['videoId'],
            'title': item['snippet']['title'],
            'thumbnail': item['snippet']['thumbnails'].get('medium', {}).get('url', ''),
        } for item in videos_response.get('items', [])]
        return JsonResponse({'videos': videos, 'count': len(videos)})
    except Exception as e:
        logger.error(f"Playlist videos error: {e}")
        return JsonResponse({'error': 'Failed to fetch videos'}, status=400)


@login_required
def fetch_playlist_task(request, playlist_id):
    return JsonResponse({'status': 'task_started'})

@login_required
def upload_cookies_view(request):
    """
    Upload/paste YouTube cookies.txt for yt-dlp bot-detection bypass.
    """
    account = YouTubeAccount.objects.filter(user=request.user, is_active=True).first()
    if not account:
        messages.error(request, "Connect your YouTube account first.")
        return redirect("connect_youtube")
 
    if request.method == "POST":
        cookies_content = request.POST.get("cookies_txt", "").strip()
        uploaded_file = request.FILES.get("cookies_file")
 
        if uploaded_file:
            try:
                cookies_content = uploaded_file.read().decode("utf-8").strip()
            except Exception as e:
                messages.error(request, f"Could not read uploaded file: {e}")
                return redirect("upload_cookies")
 
        if not cookies_content:
            messages.error(request, "No cookies provided.")
            return redirect("upload_cookies")
 
        if "youtube.com" not in cookies_content:
            messages.error(
                request,
                "Invalid cookies â€” must contain YouTube cookies. "
                "Export while on youtube.com."
            )
            return redirect("upload_cookies")
 
        account.cookies_txt = cookies_content
        account.save(update_fields=["cookies_txt"])
        messages.success(request, "âœ… YouTube cookies saved! Playlist streaming will now work.")
        logger.info(f"Cookies uploaded for account {account.channel_id}")
        return redirect("upload_cookies")
 
    return render(request, "streaming/upload_cookies.html", {
        "account": account,
        "has_cookies": account.has_cookies(),
        "cookies_preview": (account.cookies_txt[:200] + "...") if account.has_cookies() else "",
    })
 
 
@login_required
def cookies_status_api(request):
    """API: check if cookies are uploaded."""
    account = YouTubeAccount.objects.filter(user=request.user, is_active=True).first()
    return JsonResponse({
        "has_cookies": account.has_cookies() if account else False,
        "channel_title": account.channel_title if account else None,
    })

@login_required
@require_POST
def stream_update_thumbnail(request, stream_id):
    """Update a stream's thumbnail image."""
    stream = get_object_or_404(Stream, id=stream_id, user=request.user)
    
    thumbnail_file = request.FILES.get('thumbnail')
    if not thumbnail_file:
        messages.error(request, 'No thumbnail file provided')
        return redirect('stream_detail', stream_id=stream.id)
    
    try:
        # Simple validation: check file type
        if not thumbnail_file.content_type.startswith('image/'):
            raise ValueError('File must be an image')
        
        # Delete old thumbnail if exists
        if stream.thumbnail:
            try:
                stream.thumbnail.delete(save=False)
            except Exception:
                pass
        
        stream.thumbnail = thumbnail_file
        stream.save(update_fields=['thumbnail'])
        messages.success(request, 'Thumbnail updated successfully')
    except Exception as e:
        messages.error(request, f'Failed to update thumbnail: {str(e)}')
    
    return redirect('stream_detail', stream_id=stream.id)
