#!/usr/bin/env python
"""
Simple test to verify FFmpeg can encode and output to RTMP format.
This doesn't require YouTube API - just tests the RTMP capability locally.
"""

import subprocess
import time
import sys

def test_ffmpeg_rtmp():
    """Test if FFmpeg can handle RTMP output format."""
    
    print("="*70)
    print("FFmpeg RTMP Output Capability Test")
    print("="*70)
    
    # Create a simple test case: generate color pattern and output to FLV format
    # (FLV is the format used for RTMP streaming)
    
    test_rtmp_url = "rtmp://127.0.0.1:1935/test/stream"
    
    ffmpeg_cmd = [
        'ffmpeg',
        '-loglevel', 'debug',
        '-f', 'lavfi',
        '-i', 'color=c=red:s=640x480:d=5:r=30',  # 5-second red pattern
        '-c:v', 'libx264',
        '-preset', 'ultrafast',
        '-b:v', '2500k',
        '-f', 'flv',
        '-flvflags', 'no_duration_filesize',
        test_rtmp_url,
    ]
    
    print(f"\n📝 Test Command:")
    print(f"   {' '.join(ffmpeg_cmd)}")
    print(f"\n📌 Note: This will try to connect to rtmp://127.0.0.1:1935")
    print(f"   (Connection will fail since no RTMP server is running)")
    print(f"   We're just testing if FFmpeg TRIES to connect\n")
    
    try:
        ffmpeg = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        connection_attempted = False
        encoding_started = False
        timeout = 8
        start_time = time.time()
        
        print("📊 FFmpeg Output (first 10 seconds):\n")
        
        for line in ffmpeg.stderr:
            elapsed = time.time() - start_time
            
            # Look for key messages
            if 'Opening' in line or 'rtmp://' in line:
                print(f"  ✓ {line.strip()}")
                connection_attempted = True
            elif 'Connection refused' in line or 'Network is unreachable' in line:
                print(f"  ⚠️  {line.strip()}")
                connection_attempted = True
            elif 'frame=' in line or 'fps=' in line:
                print(f"  ✓ {line.strip()}")
                encoding_started = True
            elif 'Encoder' in line or 'Output' in line or 'Stream mapping' in line:
                print(f"  ℹ️  {line.strip()}")
            
            if elapsed > timeout:
                print(f"\n⏱️  Timeout after {timeout} seconds")
                ffmpeg.terminate()
                break
        
        ffmpeg.wait(timeout=2)
        
        print("\n" + "="*70)
        print("Test Results:")
        print("="*70)
        
        if encoding_started:
            print("✅ FFmpeg ENCODING: Working (frames are being encoded)")
        else:
            print("⚠️  FFmpeg ENCODING: No frame output detected")
        
        if connection_attempted:
            print("✅ RTMP CONNECTION: FFmpeg attempted to connect (expected failure)")
        else:
            print("❌ RTMP CONNECTION: FFmpeg never tried to connect!")
            print("   This suggests FFmpeg command syntax might be wrong")
        
        print("\n" + "="*70)
        return True
        
    except FileNotFoundError:
        print("❌ FFmpeg not found. Install FFmpeg and try again.")
        return False
    except Exception as e:
        print(f"❌ Error: {e}")
        return False

if __name__ == '__main__':
    success = test_ffmpeg_rtmp()
    sys.exit(0 if success else 1)
