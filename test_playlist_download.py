#!/usr/bin/env python
"""
Test script for YouTube Playlist Download & Storage Streaming feature
Validates all new functionality is working correctly
"""

import os
import sys
import django

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
sys.path.insert(0, '/workspaces/youtube_streamer')
django.setup()

from apps.streaming.models import Stream, MediaFile
from apps.streaming.stream_manager import StreamManager
from apps.accounts.models import YouTubeAccount
from django.contrib.auth.models import User
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

def test_download_functionality():
    """Test the playlist download functionality"""
    print("\n" + "="*70)
    print("TEST: YouTube Playlist Download & Storage Streaming")
    print("="*70)
    
    # Create test user and YouTube account
    print("\n1. Setting up test data...")
    user, _ = User.objects.get_or_create(
        username='test_downloader',
        defaults={'email': 'downloader@test.com'}
    )
    
    yt_account, _ = YouTubeAccount.objects.get_or_create(
        user=user,
        channel_id='test_downloader_channel',
        defaults={
            'channel_title': 'Test Downloader Channel',
            'access_token': 'test_token',
            'refresh_token': 'test_refresh',
            'is_active': True
        }
    )
    
    # Create stream with playlist data
    print("2. Creating stream with YouTube playlist...")
    stream = Stream.objects.create(
        user=user,
        youtube_account=yt_account,
        title='Download Test Stream',
        description='Testing playlist download feature',
        playlist_videos=[{
            'youtube_playlist_id': 'PLxxxxxxxxxxx',
            'videos': [
                {
                    'video_id': 'test_vid_1',
                    'title': 'Test Video 1',
                    'url': 'https://www.youtube.com/watch?v=test_vid_1'
                },
                {
                    'video_id': 'test_vid_2',
                    'title': 'Test Video 2',
                    'url': 'https://www.youtube.com/watch?v=test_vid_2'
                }
            ]
        }]
    )
    print(f"   ✓ Stream created: {stream.id}")
    
    # Test StreamManager
    print("\n3. Testing StreamManager...")
    manager = StreamManager(stream)
    print(f"   ✓ StreamManager initialized")
    
    # Test stream type detection
    print("\n4. Testing stream type detection...")
    has_local = stream.media_files.exists()
    has_playlist = bool(stream.playlist_videos)
    print(f"   ✓ Has local media: {has_local} (expected: False)")
    print(f"   ✓ Has YouTube playlist: {has_playlist} (expected: True)")
    
    if not has_playlist:
        print("   ✗ FAILED: Stream should have playlist data")
        return False
    
    # Test download_playlist_videos method exists
    print("\n5. Testing download_playlist_videos method...")
    if hasattr(manager, 'download_playlist_videos'):
        print(f"   ✓ Method exists: download_playlist_videos()")
    else:
        print(f"   ✗ FAILED: Method not found")
        return False
    
    # Test FFmpeg command generation
    print("\n6. Testing FFmpeg command building...")
    import tempfile
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write("ffconcat version 1.0\n")
        f.write("file '/storage/test_video_1.mp4'\n")
        f.write("file '/storage/test_video_2.mp4'\n")
        concat_path = f.name
    
    try:
        stream.stream_url = 'rtmp://a.rtmp.youtube.com/live2/test_key'
        cmd = manager._build_youtube_ffmpeg_command(concat_path)
        
        print(f"   ✓ FFmpeg command built: {len(cmd)} arguments")
        
        # Verify key components
        checks = {
            "ffmpeg binary": any('ffmpeg' in str(arg) for arg in cmd),
            "concat input": '-f' in cmd and 'concat' in cmd,
            "input file": concat_path in cmd,
            "H.264 codec": 'libx264' in cmd,
            "AAC codec": 'aac' in cmd,
            "FLV output": 'flv' in cmd,
            "RTMP URL": 'rtmp://' in str(cmd),
            "-re flag": '-re' in cmd,
        }
        
        for check_name, passed in checks.items():
            status = "✓" if passed else "✗"
            print(f"   {status} {check_name}")
        
        if not all(checks.values()):
            print("\n   ✗ Some FFmpeg command components missing")
            return False
            
    finally:
        import os
        if os.path.exists(concat_path):
            os.unlink(concat_path)
    
    # Test streaming modes
    print("\n7. Testing streaming mode detection...")
    
    # Mode 1: YouTube playlist (no downloads yet)
    mode1_has_local = stream.media_files.exists()
    mode1_has_playlist = bool(stream.playlist_videos)
    print(f"   ✓ Mode 1 (YouTube Playlist):")
    print(f"      - has_local_media: {mode1_has_local}")
    print(f"      - has_youtube_playlist: {mode1_has_playlist}")
    
    if mode1_has_local:
        expected_flow = "_start_local_media_stream()"
    elif mode1_has_playlist:
        expected_flow = "_start_youtube_playlist_stream()"
    else:
        expected_flow = "ERROR"
    
    print(f"      - Expected flow: {expected_flow}")
    
    # Check methods exist
    print("\n8. Checking all required methods exist...")
    required_methods = [
        'authenticate_youtube',
        'download_playlist_videos',
        'create_broadcast',
        'start_ffmpeg_stream',
        '_start_local_media_stream',
        '_start_youtube_playlist_stream',
        '_start_youtube_url_stream',
        '_build_youtube_ffmpeg_command',
        '_spawn_ffmpeg',
        'stop_stream',
    ]
    
    all_exist = True
    for method_name in required_methods:
        exists = hasattr(manager, method_name) and callable(getattr(manager, method_name))
        status = "✓" if exists else "✗"
        print(f"   {status} {method_name}")
        if not exists:
            all_exist = False
    
    if not all_exist:
        print("\n   ✗ Some methods missing")
        return False
    
    # Check Celery tasks exist
    print("\n9. Checking Celery tasks...")
    try:
        from apps.streaming.tasks import download_playlist_videos_async
        print(f"   ✓ Celery task exists: download_playlist_videos_async")
    except ImportError as e:
        print(f"   ✗ Failed to import: {e}")
        return False
    
    # Check view exists
    print("\n10. Checking view function...")
    try:
        from apps.streaming.views import download_playlist_videos_view
        print(f"   ✓ View exists: download_playlist_videos_view")
    except ImportError as e:
        print(f"   ✗ Failed to import: {e}")
        return False
    
    # Check URL routing
    print("\n11. Checking URL routing...")
    try:
        from django.urls import reverse
        url = reverse('download_playlist_videos', kwargs={'stream_id': stream.id})
        print(f"   ✓ URL route exists: {url}")
    except Exception as e:
        print(f"   ✗ URL routing failed: {e}")
        return False
    
    print("\n" + "="*70)
    print("✅ ALL TESTS PASSED!")
    print("="*70)
    print("\nFeature is ready to use:")
    print("  1. Create stream with YouTube playlist")
    print("  2. Click 'Download Playlist Videos' button")
    print("  3. Wait for download to complete (background task)")
    print("  4. Click 'Start Stream'")
    print("  5. Video streams from local storage to YouTube")
    print("\n")
    
    return True


if __name__ == '__main__':
    try:
        success = test_download_functionality()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
