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
from .stream_manager import StreamManager

logger = logging.getLogger(__name__)

# ============ HELPERS ============

def get_user_storage_usage(user) -> int:
    """Calculate total storage used by user in bytes"""
    return sum(
        media.file_size for media in MediaFile.objects.filter(user=user)
        if media.file_size
    ) or 0


def has_storage_available(user, file_size: int) -> Tuple[bool, int, int]:
    """Check if user has storage available for new file"""
    subscription = Subscription.objects.filter(
        user=user,
        is_active=True,
        status='active'
    ).first()

    if not subscription:
        return False, 0, 0

    current_usage = get_user_storage_usage(user)
    available_storage = subscription.storage_limit - current_usage

    if file_size > available_storage:
        return False, current_usage, subscription.storage_limit

    return True, current_usage, subscription.storage_limit


def format_bytes(bytes_size: int) -> str:
    """Convert bytes to human readable format"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024:
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.2f} TB"


def validate_file_upload(uploaded_file):
    """Validate file content (not just extension)"""
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
    """Reorder media files"""
    try:
        data = json.loads(request.body)
        order = data.get('order', [])
        
        for item in order:
            media_id = item['id']
            sequence = item['sequence']
            MediaFile.objects.filter(
                id=media_id,
                user=request.user
            ).update(sequence=sequence)
        
        return JsonResponse({'status': 'success'})
    except Exception as e:
        logger.error(f"Reorder failed: {e}")
        return JsonResponse({'status': 'error'}, status=400)


@login_required
def connect_youtube(request):
    """Initiate YouTube OAuth flow"""
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
    """Handle YouTube OAuth callback with CSRF validation"""
    try:
        # VALIDATE CSRF
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
            part='snippet,contentDetails',
            mine=True
        ).execute()

        if channel_response['items']:
            channel = channel_response['items'][0]
            channel_id = channel['id']
            channel_title = channel['snippet']['title']
            
            youtube_account, created = YouTubeAccount.objects.update_or_create(
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
            
            logger.warning(f'OAuth_ACCOUNT_CONNECTED',
                extra={'user_id': request.user.id, 'channel_id': channel_id}
            )
            messages.success(request, f'Connected: {channel_title}')
        else:
            messages.error(request, 'No YouTube channel found')
            
    except Exception as e:
        logger.error(f"OAuth callback failed: {e}")
        messages.error(request, 'Connection failed')
    
    finally:
        request.session.pop('oauth_state', None)
    
    return redirect('dashboard')

def create_youtube_playlist_file(stream):
    """✅ WORKING: Use yt-dlp to download first video for testing"""
    stream_dir = get_temp_dir_for_stream(stream.id)
    os.makedirs(stream_dir, exist_ok=True)
    
    # Get first video ID from playlist
    playlist_data = stream.playlist_videos[0]
    playlist_id = playlist_data['youtube_playlist_id']
    
    print(f"🔍 Processing playlist: {playlist_id}")
    
    # STEP 1: Get first video URL with yt-dlp
    cmd = [
        'yt-dlp', 
        '--playlist-items', '1',  # ONLY FIRST VIDEO
        '--get-url',
        f'https://www.youtube.com/playlist?list={playlist_id}'
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    print(f"yt-dlp stdout: {result.stdout}")
    print(f"yt-dlp stderr: {result.stderr}")
    
    if result.returncode != 0 or not result.stdout.strip():
        # FALLBACK: Use first video from playlist data
        first_video = playlist_data['videos'][0]['video_id']
        video_url = f"https://www.youtube.com/watch?v={first_video}"
        print(f"🔧 FALLBACK: Using video {first_video}")
    else:
        video_url = result.stdout.strip().split('\n')[0]
    
    # STEP 2: DOWNLOAD VIDEO (5-30 seconds)
    video_path = os.path.join(stream_dir, 'video.mp4')
    download_cmd = [
        'yt-dlp',
        '-f', 'best[height<=720]',  # 720p max
        '--no-playlist',
        '-o', video_path,
        video_url
    ]
    
    print(f"📥 Downloading: {video_url}")
    download_result = subprocess.run(download_cmd, capture_output=True, timeout=120)
    
    if download_result.returncode != 0 or not os.path.exists(video_path):
        raise Exception(f"Download failed: {download_result.stderr}")
    
    print(f"✅ VIDEO READY: {video_path} ({os.path.getsize(video_path)/1024/1024:.1f}MB)")
    
    # STEP 3: Create infinite loop playlist
    concat_path = os.path.join(stream_dir, 'playlist.txt')
    with open(concat_path, 'w') as f:
        f.write("ffconcat version 1.0\n")
        f.write(f"file '{video_path}'\n")  # Loop this file infinitely
    
    return concat_path

@login_required
def test_broadcast(request, stream_id):
    stream = get_object_or_404(Stream, id=stream_id, user=request.user)
    
    manager = StreamManager(stream)
    broadcast_id = manager.create_broadcast()
    
    if broadcast_id:
        messages.success(request, f'✅ Broadcast created: {broadcast_id}')
        messages.info(request, f'Stream URL: {stream.stream_url}')
    else:
        messages.error(request, '❌ Broadcast FAILED')
    
    return redirect('stream_detail', stream_id=stream.id)
@login_required
def stream_list(request):
    """List all user streams with optimized queries"""
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

    # NEW: Get user's playlists
    playlists= []
    account = YouTubeAccount.objects.filter(user=request.user, is_active=True).first()
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
            title = request.POST.get('title')
            description = request.POST.get('description', '')
            youtube_account_id = request.POST.get('youtube_account')
            playlist_id = request.POST.get('playlist_id')  # YouTube playlist ID string
            scheduled_start_time_str = request.POST.get('scheduled_start_time')  # ISO format string
        
            youtube_account = YouTubeAccount.objects.get(id=youtube_account_id, user=request.user)
        
            # Parse scheduled start time if provided
            scheduled_start_time = None
            stream_status = 'idle'
            
            if scheduled_start_time_str:
                try:
                    # Parse ISO format: "2026-02-15T14:30"
                    from datetime import datetime
                    scheduled_start_time = datetime.fromisoformat(scheduled_start_time_str)
                    # Make it timezone-aware
                    if scheduled_start_time.tzinfo is None:
                        from django.utils import timezone
                        scheduled_start_time = timezone.make_aware(scheduled_start_time)
                    stream_status = 'scheduled'
                except Exception as e:
                    logger.error(f"Failed to parse scheduled time: {e}")
                    messages.warning(request, "Invalid scheduled time, creating in idle state")
            
            # ✅ FIX: Create/get Playlist object first
            #playlist = get_or_create_playlist(request.user, playlist_id, youtube_account)
        
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
                messages.success(request, f'Stream "{title}" scheduled to start at {scheduled_start_time}')
            else:
                messages.success(request, f'Stream "{title}" created!')
            return redirect('stream_detail', stream_id=stream.id)
    
        except Exception as e:
            messages.error(request, f'Error: {str(e)}')
    return render(request, 'streaming/stream_create.html', {
        'youtube_accounts': youtube_accounts,
        'playlists': playlists,  # NEW

    })

'''
@login_required
@transaction.atomic
def stream_create(request):
    """Create a new stream with atomic constraint check"""
    subscription = Subscription.objects.filter(
        user=request.user,
        is_active=True
    ).select_for_update().first()

    if not subscription:
        messages.error(request, 'You need an active subscription')
        return redirect('subscribe')

    # Check stream limit WITHIN transaction
    active_streams = Stream.objects.filter(
        user=request.user,
        status__in=['running', 'starting', 'scheduled']
    ).count()

    if active_streams >= subscription.max_streams:
        messages.error(
            request,
            f'Stream limit reached ({subscription.max_streams} active streams)'
        )
        return redirect('stream_list')

    youtube_accounts = YouTubeAccount.objects.filter(user=request.user, is_active=True)
    if not youtube_accounts.exists():
        messages.error(request, 'Connect YouTube first')
        return redirect('connect_youtube')

    if request.method == 'POST':
        try:
            title = request.POST.get('title', '').strip()
            description = request.POST.get('description', '').strip()
            youtube_account_id = request.POST.get('youtube_account')
            media_file_ids = request.POST.getlist('media_files')
            thumbnail = request.FILES.get('thumbnail')

            # Validate input
            if not title or len(title) > 255:
                messages.error(request, 'Invalid title')
                return redirect('stream_create')
            
            if len(description) > 5000:
                messages.error(request, 'Description too long')
                return redirect('stream_create')

            youtube_account = YouTubeAccount.objects.get(
                id=youtube_account_id,
                user=request.user
            )

            stream = Stream.objects.create(
                user=request.user,
                youtube_account=youtube_account,
                title=title,
                description=description,
                thumbnail=thumbnail
            )

            if media_file_ids:
                media_files = MediaFile.objects.filter(
                    id__in=media_file_ids,
                    user=request.user
                )
                stream.media_files.set(media_files)

            messages.success(request, 'Stream created')
            return redirect('stream_detail', stream_id=stream.id)

        except IntegrityError:
            messages.error(request, 'Stream limit reached')
            return redirect('stream_list')
        except Exception as e:
            logger.error(f"Stream creation failed: {e}")
            messages.error(request, 'Creation failed')

    current_usage = get_user_storage_usage(request.user)
    available = subscription.storage_limit - current_usage

    return render(request, 'streaming/stream_create.html', {
        'youtube_accounts': youtube_accounts,
        'media_files': MediaFile.objects.filter(user=request.user),
        'storage_usage': format_bytes(current_usage),
        'storage_limit': format_bytes(subscription.storage_limit),
        'storage_available': format_bytes(available),
    })
'''

@login_required
def stream_detail(request, stream_id):
    """View stream details with optimized queries"""
    stream = get_object_or_404(Stream, id=stream_id, user=request.user)
    logs = stream.logs.all()[:50]
    
    return render(request, 'streaming/stream_detail.html', {
        'stream': stream,
        'logs': logs,
    })


@login_required
@require_POST
def download_playlist_videos_view(request, stream_id):
    """Download YouTube playlist videos and store as MediaFile objects"""
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
        # Import Celery task
        from .tasks import download_playlist_videos_async
        
        # Start async download task
        max_videos = int(request.POST.get('max_videos', 50))
        task = download_playlist_videos_async.delay(str(stream.id), max_videos)
        
        messages.success(request, f'Download started (Task ID: {task.id})')
        
        StreamLog.objects.create(
            stream=stream,
            level='INFO',
            message=f'Started downloading playlist videos (max {max_videos})'
        )
        
        logger.info(f"📥 Download task started for stream {stream.id}: {task.id}")
    
    except Exception as e:
        logger.error(f"Failed to start download: {e}")
        messages.error(request, f'Download failed: {str(e)}')
    
    return redirect('stream_detail', stream_id=stream.id)


@login_required
@ratelimit(key='user', rate='5/h', method='POST')
@login_required
def stream_start(request, stream_id):
    """Start a stream"""
    stream = get_object_or_404(Stream, id=stream_id, user=request.user)

    if stream.status == 'running':
        messages.warning(request, 'Stream is already running')
        return redirect('stream_detail', stream_id=stream.id)

    try:
        stream.status = 'starting'
        stream.save()

        manager = StreamManager(stream)

        # Create YouTube broadcast
        broadcast_id = manager.create_broadcast()
        if not broadcast_id:
            raise Exception("Failed to create YouTube broadcast")

        # Start FFmpeg streaming
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
@ratelimit(key='user', rate='5/h', method='POST')
def stream_stop(request, stream_id):
    """🎬 STOP STREAM - Gracefully end stream + YouTube broadcast"""
    stream = get_object_or_404(Stream, id=stream_id, user=request.user)

    try:
        stream.status = 'stopping'
        stream.save()

        manager = StreamManager(stream)
        
        # This calls: _end_youtube_broadcast() then _graceful_ffmpeg_stop()
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
    """Delete a stream"""
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
@transaction.atomic
def media_upload_view(request):
    """Upload media with validation + atomic storage check"""
    subscription = Subscription.objects.filter(
        user=request.user,
        is_active=True,
        status='active'
    ).select_for_update().first()

    if not subscription:
        messages.error(request, 'No active subscription')
        return redirect('subscribe')

    if request.method == 'POST':
        try:
            file = request.FILES.get('file')
            title = request.POST.get('title', '').strip()

            # Validate file content
            validate_file_upload(file)

            # Check storage atomically
            has_storage, current, limit = has_storage_available(
                request.user,
                file.size
            )

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
    """List media with optimized queries"""
    media_files = MediaFile.objects.filter(
        user=request.user
    ).order_by('-created_at')

    subscription = Subscription.objects.filter(
        user=request.user,
        is_active=True
    ).first()

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
    """Delete media and update storage"""
    media = get_object_or_404(MediaFile, id=media_id, user=request.user)

    freed = media.file_size or 0
    media.file.delete(save=False)
    media.thumbnail.delete(save=False)
    media.delete()

    subscription = Subscription.objects.filter(
        user=request.user,
        is_active=True
    ).first()

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
    """API: Get stream status"""
    stream = get_object_or_404(Stream, id=stream_id, user=request.user)

    return JsonResponse({
        'status': stream.status,
        'started_at': stream.started_at.isoformat() if stream.started_at else None,
        'uptime_seconds': stream.uptime_seconds,
        'error_message': stream.error_message,
        'is_process_alive': stream.is_process_alive(),
    })

# Add after oauth_callback view:

@login_required
def user_playlists_api(request):
    """API: List user's playlists"""
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
    """API: Get videos from playlist"""
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
        
        videos = []
        for item in videos_response['items']:
            videos.append({
                'video_id': item['contentDetails']['videoId'],
                'title': item['snippet']['title'],
                'thumbnail': item['snippet']['thumbnails']['medium']['url'],
                'duration': 'Unknown'  # Add contentDetails.duration if needed
            })
        
        return JsonResponse({'videos': videos, 'count': len(videos)})
    except Exception as e:
        logger.error(f"Playlist videos error: {e}")
        return JsonResponse({'error': 'Failed to fetch videos'}, status=400)

@login_required
def fetch_playlist_task(request, playlist_id):
    """Trigger Celery task to process playlist"""
    # Later: from .tasks import process_playlist
    # process_playlist.delay(request.user.id, playlist_id)
    return JsonResponse({'status': 'task_started'})
