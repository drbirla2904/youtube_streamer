# Streaming Error Fix Summary

## Problem
Error: "Failed to start stream: Failed to start streaming process"

## Root Cause
**Critical Bug in `_build_youtube_ffmpeg_command` method**: 
- The method referenced an undefined variable `video_path` instead of using the `concat_path` parameter
- This caused a `NameError` when building the FFmpeg command
- The exception was caught silently, causing `start_ffmpeg_stream()` to return `None`
- This triggered the "Failed to start streaming process" error in views.py and tasks.py

## Fixes Applied

### 1. **Fixed FFmpeg Command Builder** (`stream_manager.py` line 505-535)
**Before:**
```python
return [
    'ffmpeg',
    '-loglevel', 'info',
    '-stream_loop', '-1',
    '-i', video_path,  # ❌ UNDEFINED VARIABLE
    ...
]
```

**After:**
```python
ffmpeg_bin = resolve_ffmpeg_binary()
return [
    ffmpeg_bin,  # Use resolved binary path
    '-loglevel', 'info',
    '-f', 'concat',  # Use concat demuxer
    '-safe', '0',
    '-i', concat_path,  # ✅ Correct variable
    ...
]
```

### 2. **Enhanced Error Logging in `start_ffmpeg_stream`** (line 428-461)
- Added detailed logging at each step
- Added null check for FFmpeg process
- Added timestamp tracking (started_at, process_started_at)
- Better exception reporting with `exc_info=True`

### 3. **Improved `create_youtube_playlist_file` Robustness** (line 166-218)
- Added validation for playlist data structure
- Better error messages for debugging
- Handles both list and dict formats
- Validates YouTube account exists
- Validates videos were fetched
- Added debug logging for each video

## Verification

### FFmpeg Installation ✓
```
ffmpeg version 6.1.1-3ubuntu5
- libx264 enabled (required for video encoding)
- All necessary codecs available
- Located at: /usr/bin/ffmpeg
```

## Remaining Considerations

### Important Architectural Note
The current implementation uses FFmpeg's concat demuxer with YouTube URLs:
```
file 'https://www.youtube.com/watch?v=VIDEO_ID'
```

**Potential Limitation:** FFmpeg's concat demuxer may have limitations with remote URLs. If issues persist, consider:

1. **Using youtube-dl/yt-dlp** for direct download links:
   ```python
   import subprocess
   result = subprocess.run(['yt-dlp', '-f', 'best', '-g', video_url], capture_output=True)
   direct_url = result.stdout.decode().strip()
   ```

2. **Pre-downloading videos** before passing to FFmpeg

3. **Using HLS playlist input** instead of concat demuxer

## Testing the Fix

### Basic test to verify the stream starts:
```python
from apps.streaming.models import Stream
from apps.streaming.stream_manager import StreamManager

stream = Stream.objects.get(id=stream_id)
manager = StreamManager(stream)

# Test broadcast creation
broadcast_id = manager.create_broadcast()
print(f"Broadcast ID: {broadcast_id}")

# Test stream start
pid = manager.start_ffmpeg_stream()
print(f"FFmpeg PID: {pid}")
```

### Check logs:
```bash
tail -f logs/django.log | grep -i "stream\|ffmpeg"
```

## Files Modified
- `/workspaces/youtube_streamer/apps/streaming/stream_manager.py`
  - `_build_youtube_ffmpeg_command()` - Fixed undefined variable
  - `start_ffmpeg_stream()` - Added better logging
  - `create_youtube_playlist_file()` - Improved error handling

## Next Steps if Still Having Issues

1. Check Django logs for detailed error messages
2. Verify YouTube account has valid OAuth tokens
3. Ensure stream.playlist_videos is properly set with playlist ID
4. Check if FFmpeg can access YouTube URLs (may need --no-check-certificate or proxy)
5. Monitor FFmpeg stderr output in logs for specific encoding errors
