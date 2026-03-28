#!/usr/bin/env python
"""
Test script to verify FFmpeg can connect to YouTube's RTMP endpoint.
This will help diagnose why liveStream remains in 'ready' status.
"""

import os
import sys
import django
import subprocess
import time
import logging
from datetime import datetime, timedelta

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.accounts.models import YouTubeAccount
from apps.streaming.models import Stream
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

def test_rtmp_connection():
    """Test if FFmpeg can connect to YouTube's RTMP endpoint."""
    
    # Get authenticated account
    account = YouTubeAccount.objects.filter(is_active=True).first()
    if not account:
        logger.error("❌ No active YouTube account found")
        return False
    
    logger.info(f"✓ Using YouTube account: {account.channel_id}")
    
    # Create YouTube service
    creds = Credentials(token=account.access_token)
    yt = build('youtube', 'v3', credentials=creds)
    
    try:
        # Create a test broadcast
        logger.info("Creating test broadcast...")
        broadcast = yt.liveBroadcasts().insert(
            part='snippet,status,contentDetails',
            body={
                'snippet': {
                    'title': f'TEST - RTMP Connection Check {datetime.now().isoformat()}',
                    'description': 'Diagnostic test for RTMP connectivity',
                    'scheduledStartTime': (datetime.utcnow() + timedelta(seconds=30)).isoformat() + 'Z',
                },
                'status': {'privacyStatus': 'unlisted', 'selfDeclaredMadeForKids': False},
                'contentDetails': {
                    'enableAutoStart': True,
                    'enableAutoStop': True,
                    'enableDvr': False,
                }
            }
        ).execute()
        
        broadcast_id = broadcast['id']
        logger.info(f"✓ Broadcast created: {broadcast_id}")
        
        # Create liveStream
        logger.info("Creating liveStream...")
        stream_resp = yt.liveStreams().insert(
            part='snippet,cdn,status',
            body={
                'snippet': {'title': f'TEST - RTMP Test {datetime.now().isoformat()}'},
                'cdn': {'frameRate': 'variable', 'ingestionType': 'rtmp', 'resolution': 'variable'}
            }
        ).execute()
        
        stream_id = stream_resp['id']
        stream_key = stream_resp['cdn']['ingestionInfo']['streamName']
        ingestion_addr = stream_resp['cdn']['ingestionInfo']['ingestionAddress']
        rtmp_url = f"{ingestion_addr}/{stream_key}"
        
        logger.info(f"✓ liveStream created: {stream_id}")
        logger.info(f"  RTMP URL: {rtmp_url}")
        logger.info(f"  Stream Key: {stream_key}")
        logger.info(f"  Ingestion Addr: {ingestion_addr}")
        
        # Bind broadcast to stream
        logger.info("Binding broadcast to liveStream...")
        yt.liveBroadcasts().bind(
            part='id,contentDetails',
            id=broadcast_id,
            streamId=stream_id
        ).execute()
        logger.info("✓ Broadcast bound to liveStream")
        
        # Check initial status
        time.sleep(2)
        check_status(yt, stream_id, broadcast_id, "Initial status")
        
        # Start test pattern with FFmpeg
        logger.info("\n🎬 Starting FFmpeg pattern test (30 seconds)...")
        logger.info("   This will send a color pattern to YouTube RTMP endpoint")
        
        ffmpeg_cmd = [
            'ffmpeg',
            '-loglevel', 'debug',
            '-f', 'lavfi', '-i', 'color=c=blue:s=1280x720:d=30:r=30',  # 30s blue pattern
            '-c:v', 'libx264', '-preset', 'veryfast', '-b:v', '2500k',
            '-f', 'flv', '-flvflags', 'no_duration_filesize',
            rtmp_url
        ]
        
        logger.debug(f"FFmpeg command: {' '.join(ffmpeg_cmd)}")
        
        try:
            ffmpeg = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            # Monitor output for RTMP connection
            logger.info("Monitoring FFmpeg output for RTMP connection...")
            rtmp_connected = False
            output_lines = []
            
            for line in ffmpeg.stderr:
                output_lines.append(line.strip())
                if 'RTMP' in line or 'rtmp' in line or 'Connection' in line or 'connected' in line.lower():
                    logger.info(f"FFmpeg: {line.strip()}")
                    rtmp_connected = True
            
            ffmpeg.wait(timeout=35)
            
        except subprocess.TimeoutExpired:
            ffmpeg.terminate()
            logger.warning("FFmpeg timeout (expected - continuing with status check)")
        except Exception as e:
            logger.error(f"FFmpeg error: {e}")
        
        # Final status check
        logger.info("\n📊 Final status check...")
        time.sleep(3)
        check_status(yt, stream_id, broadcast_id, "After RTMP test")
        
        # Cleanup
        logger.info("\n🧹 Cleaning up test broadcast...")
        try:
            yt.liveBroadcasts().delete(id=broadcast_id).execute()
            logger.info("✓ Broadcast deleted")
        except Exception as e:
            logger.warning(f"Could not delete broadcast: {e}")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Error during RTMP test: {e}", exc_info=True)
        return False

def check_status(yt, stream_id, broadcast_id, label):
    """Check and log current status of broadcast and stream."""
    try:
        # Check broadcast status
        b_resp = yt.liveBroadcasts().list(
            part='status,contentDetails',
            id=broadcast_id
        ).execute()
        
        if b_resp.get('items'):
            b_status = b_resp['items'][0]['status']['broadcastStatus']
            logger.info(f"  [{label}] Broadcast status: {b_status}")
        
        # Check liveStream status
        s_resp = yt.liveStreams().list(
            part='status',
            id=stream_id
        ).execute()
        
        if s_resp.get('items'):
            s_status = s_resp['items'][0]['status']['streamStatus']
            logger.info(f"  [{label}] liveStream status: {s_status}")
            
            if s_status != 'active':
                logger.warning(f"  ⚠️  liveStream NOT 'active' (YouTube not detecting RTMP data)")
            else:
                logger.info(f"  ✅ liveStream is 'active' (RTMP connection successful!)")
        
    except Exception as e:
        logger.error(f"  Error checking status: {e}")

if __name__ == '__main__':
    logger.info("="*70)
    logger.info("YouTube RTMP Connection Diagnostic Test")
    logger.info("="*70)
    success = test_rtmp_connection()
    logger.info("="*70)
    if success:
        logger.info("✅ Test completed. Check the logs above for RTMP status.")
    else:
        logger.error("❌ Test failed.")
    sys.exit(0 if success else 1)
