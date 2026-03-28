from django.shortcuts import render, redirect
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.contrib.auth.forms import AuthenticationForm
from .forms import UserRegistrationForm, UserUpdateForm, ProfileUpdateForm
from apps.payments.models import Subscription
from django.core.exceptions import ObjectDoesNotExist
from apps.accounts.models import UserProfile

def register_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    
    if request.method == 'POST':
        form = UserRegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, 'Account created successfully!')
            return redirect('dashboard')
    else:
        form = UserRegistrationForm()
    
    return render(request, 'accounts/register.html', {'form': form})


def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    
    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            username = form.cleaned_data.get('username')
            password = form.cleaned_data.get('password')
            user = authenticate(username=username, password=password)
            if user is not None:
                login(request, user)
                messages.success(request, f'Welcome back, {username}!')
                return redirect('dashboard')
    else:
        form = AuthenticationForm()
    
    return render(request, 'accounts/login.html', {'form': form})


@login_required
def logout_view(request):
    logout(request)
    messages.info(request, 'You have been logged out.')
    return redirect('home')


@login_required
def dashboard_view(request):
    try:
        subscription = Subscription.objects.filter(
            user=request.user, 
            is_active=True
        ).order_by('-created_at').first()
    except:
        subscription = None
    
    youtube_accounts = request.user.youtube_accounts.filter(is_active=True)
    active_streams = request.user.streams.filter(status__in=['running', 'starting'])
    
    context = {
        'subscription': subscription,
        'youtube_accounts': youtube_accounts,
        'active_streams': active_streams,
    }
    return render(request, 'accounts/dashboard.html', context)

@login_required
def profile_view(request):
    try:
        profile = request.user.profile
    except ObjectDoesNotExist:
        profile = UserProfile.objects.create(user=request.user)

    if request.method == 'POST':
        user_form = UserUpdateForm(request.POST, instance=request.user)
        profile_form = ProfileUpdateForm(request.POST, instance=profile)
        
        if user_form.is_valid() and profile_form.is_valid():
            user_form.save()
            profile_form.save()
            messages.success(request, 'Profile updated successfully!')
            return redirect('profile')
    else:
        user_form = UserUpdateForm(instance=request.user)
        profile_form = ProfileUpdateForm(instance=profile)
    
    context = {
        'user_form': user_form,
        'profile_form': profile_form,
    }
    return render(request, 'accounts/profile.html', context)


from django.shortcuts import redirect, get_object_or_404
from django.contrib import messages
from apps.accounts.models import YouTubeAccount
from django.contrib.auth.decorators import login_required

from django.shortcuts import redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.conf import settings
from apps.accounts.models import YouTubeAccount
from apps.streaming.models import Stream
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import logging
import psutil

logger = logging.getLogger(__name__)

@login_required
def disconnect_youtube(request, account_id):
    """Disconnect YouTube account and stop all associated streams"""
    yt_account = get_object_or_404(YouTubeAccount, id=account_id, user=request.user)
    
    # Step 1: Build YouTube client BEFORE clearing tokens
    youtube = None
    try:
        if yt_account.access_token and yt_account.refresh_token:
            credentials = Credentials(
                token=yt_account.access_token,
                refresh_token=yt_account.refresh_token,
                token_uri='https://oauth2.googleapis.com/token',
                client_id=settings.GOOGLE_CLIENT_ID,
                client_secret=settings.GOOGLE_CLIENT_SECRET
            )
            youtube = build('youtube', 'v3', credentials=credentials)
    except Exception as e:
        logger.error(f"Failed to build YouTube client for disconnect: {e}")
    
    # Step 2: Find all running streams for this account
    active_streams = Stream.objects.filter(
        youtube_account=yt_account,
        status__in=['running', 'starting']
    )
    
    # 🔍 DEBUG LOGGING - PUT HERE
    logger.info(f"=== DISCONNECT DEBUG ===")
    logger.info(f"Account: {yt_account.channel_title}")
    logger.info(f"Found {active_streams.count()} active streams")
    logger.info(f"YouTube client built: {youtube is not None}")
    for stream in active_streams:
        logger.info(f"Stream {stream.id} - Title: {stream.title} - Broadcast ID: {stream.broadcast_id} - Process ID: {stream.process_id}")
    logger.info(f"========================")
    
    # Step 3: Stop each stream on YouTube BEFORE clearing tokens
    for stream in active_streams:
        try:
            # Kill local FFmpeg process
            if stream.process_id:
                try:
                    parent = psutil.Process(stream.process_id)
                    for child in parent.children(recursive=True):
                        child.terminate()
                    parent.terminate()
                    parent.wait(timeout=5)
                    logger.info(f"Killed FFmpeg process {stream.process_id}")
                except Exception as e:
                    logger.error(f"Failed to kill process {stream.process_id}: {e}")
                stream.process_id = None
            
            # End YouTube broadcast if we have valid credentials
            if youtube and stream.broadcast_id:
                try:
                    youtube.liveBroadcasts().transition(
                        part='status',
                        id=stream.broadcast_id,
                        broadcastStatus='complete'
                    ).execute()
                    logger.info(f"✅ Successfully ended YouTube broadcast {stream.broadcast_id}")
                except Exception as e:
                    logger.error(f"❌ Failed to end broadcast {stream.broadcast_id}: {e}")
            else:
                logger.warning(f"Skipping YouTube transition - youtube={youtube is not None}, broadcast_id={stream.broadcast_id}")
            
            # Update stream status
            stream.status = 'stopped'
            stream.save()
            
        except Exception as e:
            logger.error(f"Error stopping stream {stream.id}: {e}")
    
    # Step 4: NOW clear the YouTube account tokens
    yt_account.is_active = False
    yt_account.access_token = ""
    yt_account.refresh_token = ""
    yt_account.save()
    
    messages.success(
        request, 
        f"Disconnected {yt_account.channel_title}. {active_streams.count()} stream(s) stopped."
    )
    return redirect('dashboard')

from django.shortcuts import render

from django.shortcuts import render

def about_view(request):
    return render(request, "accounts/about.html")


def privacy_policy_view(request):
    return render(request, "accounts/policy.html")


def terms_view(request):
    return render(request, "accounts/terms.html")


def support_view(request):
    return render(request, "accounts/support.html")

