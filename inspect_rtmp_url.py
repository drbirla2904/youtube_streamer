#!/usr/bin/env python
"""
Inspect YouTube RTMP URL and test connectivity.
"""

import os
import sys
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.accounts.models import YouTubeAccount
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from datetime import datetime, timedelta
import logging

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

def inspect_rtmp_url():
    """Create a broadcast and inspect the RTMP URL format."""
    
    account = YouTubeAccount.objects.filter(is_active=True).first()
    if not account:
        logger.error("❌ No active YouTube account found")
        return False
    
    logger.info(f"✓ Using account: {account.channel_id}\n")
    
    creds = Credentials(token=account.access_token)
    yt = build('youtube', 'v3', credentials=creds)
    
    try:
        # Create broadcast
        logger.info("Creating test broadcast...")
        broadcast = yt.liveBroadcasts().insert(
            part='snippet,status,contentDetails',
            body={
                'snippet': {
                    'title': f'DIAGNOSTICS - RTMP URL Test {datetime.now().isoformat()}',
                    'description': 'Testing RTMP URL format',
                    'scheduledStartTime': (datetime.utcnow() + timedelta(seconds=30)).isoformat() + 'Z',
                },
                'status': {'privacyStatus': 'unlisted', 'selfDeclaredMadeForKids': False},
                'contentDetails': {
                    'enableAutoStart': True,
                    'enableAutoStop': True,
                }
            }
        ).execute()
        
        broadcast_id = broadcast['id']
        logger.info(f"✓ Broadcast: {broadcast_id}\n")
        
        # Create stream
        logger.info("Creating liveStream...")
        stream_resp = yt.liveStreams().insert(
            part='snippet,cdn,status',
            body={
                'snippet': {'title': f'DIAGNOSTICS - Stream {datetime.now().isoformat()}'},
                'cdn': {'frameRate': 'variable', 'ingestionType': 'rtmp', 'resolution': 'variable'}
            }
        ).execute()
        
        stream_id = stream_resp['id']
        cdn_info = stream_resp['cdn']['ingestionInfo']
        stream_key = cdn_info['streamName']
        ingestion_addr = cdn_info['ingestionAddress']
        rtmp_url = f"{ingestion_addr}/{stream_key}"
        
        logger.info(f"✓ liveStream: {stream_id}\n")
        
        # Display RTMP URL components
        logger.info("="*70)
        logger.info("RTMP URL Components:")
        logger.info("="*70)
        logger.info(f"\n📍 Ingestion Address: {ingestion_addr}")
        logger.info(f"📍 Stream Key:        {stream_key}")
        logger.info(f"\n📍 Full RTMP URL:     {rtmp_url}")
        
        # Analyze URL
        logger.info("\n" + "="*70)
        logger.info("RTMP URL Analysis:")
        logger.info("="*70)
        
        if ingestion_addr.startswith('rtmp://') or ingestion_addr.startswith('rtmps://'):
            logger.info(f"✅ Protocol: {ingestion_addr.split('://')[0].upper()} (valid)")
        else:
            logger.warning(f"⚠️  Protocol not standard: {ingestion_addr}")
        
        if len(stream_key) > 10:
            logger.info(f"✅ Stream key length: {len(stream_key)} characters (valid)")
        else:
            logger.warning(f"⚠️  Stream key very short: {len(stream_key)} characters")
        
        # Test with FFmpeg
        logger.info("\n" + "="*70)
        logger.info("Testing with FFmpeg:")
        logger.info("="*70)
        
        print(f"\nTo test this RTMP URL with FFmpeg, use:")
        print(f"\n  ffmpeg -f lavfi -i color=c=blue:s=1280x720:d=10:r=30 \\")
        print(f"    -c:v libx264 -preset veryfast -b:v 2500k \\")
        print(f"    -f flv -flvflags no_duration_filesize \\")
        print(f'    "{rtmp_url}"')
        print()
        
        # Cleanup
        try:
            yt.liveBroadcasts().delete(id=broadcast_id).execute()
            logger.info("✓ Broadcast cleaned up")
        except:
            pass
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        return False

if __name__ == '__main__':
    logger.info("="*70)
    logger.info("YouTube RTMP URL Inspection")
    logger.info("="*70 + "\n")
    success = inspect_rtmp_url()
    sys.exit(0 if success else 1)
