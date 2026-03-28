#!/usr/bin/env python
"""
Check if the YouTube channel is enabled for live streaming.
"""

import os
import sys
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.accounts.models import YouTubeAccount
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
import logging

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

def check_live_streaming_enabled():
    """Check if the channel is enabled for live streaming."""
    
    account = YouTubeAccount.objects.filter(is_active=True).first()
    if not account:
        logger.error("❌ No active YouTube account found")
        return False
    
    logger.info(f"Checking channel: {account.channel_id}\n")
    
    creds = Credentials(token=account.access_token)
    yt = build('youtube', 'v3', credentials=creds)
    
    try:
        # Get channel info
        logger.info("Fetching channel information...")
        channel = yt.channels().list(
            part='status,snippet',
            id=account.channel_id
        ).execute()
        
        if not channel.get('items'):
            logger.error("❌ Channel not found")
            return False
        
        ch = channel['items'][0]
        status = ch.get('status', {})
        snippet = ch.get('snippet', {})
        
        logger.info(f"✓ Channel: {snippet.get('title', 'N/A')}\n")
        
        logger.info("="*70)
        logger.info("Channel Status:")
        logger.info("="*70)
        
        # Check if channel is verified
        privacy_status = status.get('privacyStatus')
        logger.info(f"Privacy Status: {privacy_status}")
        
        # Try to get live streaming status
        logger.info("\nAttempting to list allowed features...")
        try:
            features = yt.channels().list(
                part='processingProgress',
                id=account.channel_id
            ).execute()
            logger.info("✓ Channel accessible")
        except Exception as e:
            logger.warning(f"Could not check processing status: {e}")
        
        # Test creating a broadcast
        logger.info("\nTesting broadcast creation capability...")
        try:
            from datetime import datetime, timedelta
            
            test_broadcast = yt.liveBroadcasts().insert(
                part='snippet,status',
                body={
                    'snippet': {
                        'title': 'Live Streaming Enabled Test',
                        'description': 'Testing if live streaming is enabled',
                        'scheduledStartTime': (datetime.utcnow() + timedelta(minutes=30)).isoformat() + 'Z',
                    },
                    'status': {'privacyStatus': 'unlisted', 'selfDeclaredMadeForKids': False},
                }
            ).execute()
            
            logger.info(f"✅ Broadcasts ENABLED - Successfully created: {test_broadcast['id']}")
            
            # Check if recording is possible
            logger.info("\nChecking advanced stream features...")
            features_list = list(status.get('features', []))
            if 'dvr' in features_list:
                logger.info("✅ DVR: Enabled")
            else:
                logger.info("⚠️  DVR: Disabled (not critical)")
            
            if 'archiveStream' in features_list:
                logger.info("✅ Archive: Enabled")
            else:
                logger.info("⚠️  Archive: Disabled (not critical)")
            
            # Cleanup
            try:
                yt.liveBroadcasts().delete(id=test_broadcast['id']).execute()
            except:
                pass
            
            logger.info("\n" + "="*70)
            logger.info("✅ LIVE STREAMING IS ENABLED ON THIS CHANNEL")
            logger.info("="*70)
            return True
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"\n❌ LIVE STREAMING IS NOT ENABLED")
            logger.error(f"Error: {error_msg}")
            
            if '403' in error_msg or 'forbidden' in error_msg.lower():
                logger.info("\n" + "="*70)
                logger.info("FIX: Enable Live Streaming on Your Channel")
                logger.info("="*70)
                logger.info("1. Go to: https://www.youtube.com/features")
                logger.info("2. Look for 'Live streaming'")
                logger.info("3. Click 'Enable'")
                logger.info("4. Verify your phone number (YouTube requirement)")
                logger.info("5. Wait 24 hours for activation")
                logger.info("="*70)
            
            return False
    
    except Exception as e:
        logger.error(f"❌ Error checking channel: {e}", exc_info=True)
        return False

if __name__ == '__main__':
    logger.info("="*70)
    logger.info("YouTube Live Streaming Permission Check")
    logger.info("="*70 + "\n")
    success = check_live_streaming_enabled()
    sys.exit(0 if success else 1)
