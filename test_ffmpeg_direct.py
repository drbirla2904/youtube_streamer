#!/usr/bin/env python
"""
Direct FFmpeg RTMP connection test
Runs FFmpeg with a test input to see actual RTMP error
"""
import subprocess
import time

FFMPEG_PATH = r'C:\ffmpeg\ffmpeg.exe'
# Use the RTMP URL from your actual stream
RTMP_URL = 'rtmp://x.rtmp.youtube.com/live2/ykye-p59q-3dde-ffu9-3xjr'

print("=" * 70)
print("🧪 FFmpeg Direct RTMP Connection Test")
print("=" * 70)

# Test 1: Simple test pattern (no input from yt-dlp)
print("\n[Test 1] Sending test pattern to RTMP for 5 seconds...")

cmd = [
    FFMPEG_PATH,
    '-v', 'debug',  # Full debug output
    '-f', 'lavfi',
    '-i', 'testsrc=duration=5:size=852x480:rate=1',  # 5-second test pattern
    '-f', 'flv', '-flvflags', 'no_duration_filesize',
    '-rtbufsize', '8M',
    '-timeout', '20000000',
    '-connect_timeout', '10000000',
    '-tcp_nodelay', '1',
    '-rtmp_live', 'live',
    RTMP_URL
]

print("\n📋 Command:")
print(" ".join(cmd))
print("\n" + "=" * 70)
print("Output:")
print("=" * 70 + "\n")

try:
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # Combine stderr with stdout
        universal_newlines=True,
        bufsize=1
    )
    
    # Print output in real-time
    start = time.time()
    for line in proc.stdout:
        print(line.rstrip())
        # Stop after 10 seconds
        if time.time() - start > 10:
            proc.terminate()
            break
    
    proc.wait(timeout=5)
    
except subprocess.TimeoutExpired:
    proc.kill()
    print("\n⚠️  Process killed after timeout")
except Exception as e:
    print(f"\n❌ Error: {e}")

print("\n" + "=" * 70)
print("🔍 Diagnosis:")
print("=" * 70)
print("""
If you see:
  ✅ "Opening ... for writing" + "Connected to server"
     → RTMP connection works! Problem is elsewhere
  
  ❌ "Connection refused / timed out"
     → Network/firewall issue or RTMP credentials wrong
  
  ❌ "Error opening output"
     → FFmpeg codec/format issue or bad URL format
  
  ❌ Nothing happens for 10+ seconds then timeout
     → FFmpeg hanging, possibly on TLS/SSL handshake
""")
