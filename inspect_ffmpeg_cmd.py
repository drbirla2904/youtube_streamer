#!/usr/bin/env python
"""
Capture and display the exact FFmpeg command being executed during streaming.
"""

import os
import sys
import django
import subprocess
import shlex

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.streaming.stream_manager import StreamManager
from apps.streaming.models import Stream
from apps.accounts.models import YouTubeAccount
import logging

logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

def show_ffmpeg_command():
    """Show what FFmpeg command would be executed."""
    
    account = YouTubeAccount.objects.filter(is_active=True).first()
    if not account:
        logger.error("No active YouTube account")
        return False
    
    # Create a test stream
    stream = Stream.objects.create(
        title="FFmpeg Command Inspection",
        description="Testing FFmpeg command construction",
    )
    
    try:
        manager = StreamManager(stream, account)
        
        # Build the pipe command (what we use for streaming)
        ffmpeg_cmd = manager._build_pipe_cmd()
        
        logger.info("="*70)
        logger.info("FFmpeg Command (as list):")
        logger.info("="*70)
        for i, arg in enumerate(ffmpeg_cmd):
            logger.info(f"  [{i:2d}] {repr(arg)}")
        
        logger.info("\n" + "="*70)
        logger.info("FFmpeg Command (shell format):")
        logger.info("="*70)
        
        # Show the command as it would be typed in shell (with proper escaping)
        shell_cmd = ' '.join(shlex.quote(arg) for arg in ffmpeg_cmd)
        logger.info(f"\n{shell_cmd}\n")
        
        logger.info("="*70)
        logger.info("Stream URL Being Used:")
        logger.info("="*70)
        logger.info(f"stream.stream_url = {repr(stream.stream_url)}")
        
        # Check if stream_url is None
        if stream.stream_url is None:
            logger.error("\n❌ FOUND ISSUE: stream.stream_url is None!")
            logger.error("   This means the RTMP URL was never set.")
            logger.error("   This happens when broadcast creation fails or is not called.")
            return False
        
        # Verify the RTMP URL format
        if not stream.stream_url.startswith('rtmp'):
            logger.warning(f"\n⚠️  WARNING: stream_url doesn't start with 'rtmp': {stream.stream_url}")
        
        # Check if URL contains special characters that might cause issues
        if '"' in stream.stream_url or "'" in stream.stream_url or ' ' in stream.stream_url:
            logger.warning(f"\n⚠️  WARNING: stream_url contains special characters: {stream.stream_url}")
        
        logger.info("\n✅ Command looks correct")
        return True
        
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        return False
    finally:
        try:
            stream.delete()
        except:
            pass

if __name__ == '__main__':
    logger.info("\n" + "="*70)
    logger.info("FFmpeg Command Inspection")
    logger.info("="*70 + "\n")
    success = show_ffmpeg_command()
    sys.exit(0 if success else 1)
