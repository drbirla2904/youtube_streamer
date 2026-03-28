# FFmpeg Video Streaming Fix - Complete Solution

## Problem Statement
**Issue:** "Still not sending video content to stream via FFmpeg to stream on YouTube"

The system was creating broadcasts on YouTube but no actual video content was being transmitted. This created blank/buffering streams that viewers couldn't watch.

## Root Cause Analysis

### The Core Issue
The `start_ffmpeg_stream()` method was **hard-coded to only handle YouTube playlists**:
- ✅ It could extract YouTube playlist videos
- ❌ But it completely ignored local media files attached to streams
- Result: Streams with local media would fail during startup

### Why This Happened  
The codebase had two separate streaming modes commented out:
1. Old code path for local media files (commented out, lines 591-620)
2. New code path for YouTube playlists (active, lines 524+)

When transitioning from one implementation to the other, **both capabilities were needed**, but only YouTube playlists remained active.

## The Fix

### Part 1: Uncomment and Enable media_files Relationship
**File:** `apps/streaming/models.py`

The `media_files` M2M relationship was commented out. Re-enabled it:
```python
media_files = models.ManyToManyField(
    MediaFile,
    related_name='streams',
    blank=True
)
```

**Migration Created:** `0004_stream_media_files.py`

### Part 2: Implement Dual-Mode Streaming Handler
**File:** `apps/streaming/stream_manager.py`

**Completely rewrote** `start_ffmpeg_stream()` to:

1. **Detect stream type** - Check if stream has local media files OR YouTube playlist
2. **Route appropriately:**
   - **Local media path** → `_start_local_media_stream()`
   - **YouTube playlist path** → `_start_youtube_playlist_stream()`

**New method signature:**
```python
def start_ffmpeg_stream(self):
    """Start FFmpeg streaming - handles both local media and YouTube playlists"""
    # Detects stream type and routes to appropriate handler
```

### Part 3: Improved FFmpeg Command
**File:** `apps/streaming/stream_manager.py` - `_build_youtube_ffmpeg_command()`

Enhanced the FFmpeg command for better streaming reliability:

```
-re                          # Read at native frame rate
-connection_timeout 5000000  # Network timeout handling
-socket_timeout 5000000      # Socket timeout handling
-reconnect 1                 # Auto-reconnect on failure
-reconnect_streamed 1        # Reconnect for streams
-rtbufsize 50M              # Large buffer for streaming
-keyint_min 60              # Consistent keyframe interval
-preset fast                # Balance speed & quality
```

## How It Works Now

### Scenario 1: Stream with Local Media Files
```
User uploads → Stream created with media_files
Start stream → Detects media_files → Calls _start_local_media_stream()
    ↓
Downloads all media files in parallel
    ↓
Creates FFmpeg concat file with local file paths
    ↓
Builds FFmpeg command with network-optimized settings
    ↓
Streams video + audio to YouTube RTMP endpoint
```

### Scenario 2: Stream with YouTube Playlist
```
User selects YouTube playlist → Stream created with playlist_videos
Start stream → Detects playlist_videos → Calls _start_youtube_playlist_stream()
    ↓
Extracts video URLs using yt-dlp
    ↓
Creates FFmpeg concat file with video URLs
    ↓
Builds FFmpeg command with network-optimized settings
    ↓
Streams video + audio to YouTube RTMP endpoint
```

## Implementation Details

### Local Media Stream Path
```python
def _start_local_media_stream(self):
    # 1. Get all media files attached to stream
    media_files = list(self.stream.media_files.all())
    
    # 2. Download files in parallel (3 concurrent downloads)
    file_paths = download_files_parallel(media_files, self.stream.id)
    
    # 3. Create concat demuxer file
    # Format: ffconcat version 1.0
    #         file '/path/to/file1.mp4'
    #         file '/path/to/file2.mp4'
    concat_path = create_concat_file(media_files, file_paths, ...)
    
    # 4. Verify concat file has content
    self._verify_concat_file(concat_path)
    
    # 5. Build FFmpeg command for RTMP output
    ffmpeg_cmd = self._build_youtube_ffmpeg_command(concat_path)
    
    # 6. Spawn FFmpeg process
    self.ffmpeg_process = self._spawn_ffmpeg(ffmpeg_cmd)
    
    # 7. Update stream status to 'running'
    self.stream.status = 'running'
    self.stream.process_id = self.ffmpeg_process.pid
    self.stream.save()
```

### YouTube Playlist Stream Path
```python
def _start_youtube_playlist_stream(self):
    # 1. Extract video URLs from YouTube playlist using yt-dlp
    concat_path = create_youtube_playlist_file(self.stream)
    
    # 2. Verify concat file has video URLs
    self._verify_concat_file(concat_path)
    
    # 3. Build FFmpeg command for RTMP output
    ffmpeg_cmd = self._build_youtube_ffmpeg_command(concat_path)
    
    # 4. Spawn FFmpeg process
    self.ffmpeg_process = self._spawn_ffmpeg(ffmpeg_cmd)
    
    # 5. Update stream status to 'running'
    self.stream.status = 'running'
    self.stream.process_id = self.ffmpeg_process.pid
    self.stream.save()
```

## FFmpeg Command Structure

```bash
ffmpeg \
  # Read at native frame rate
  -re \
  
  # Network timeouts
  -connection_timeout 5000000 \
  -socket_timeout 5000000 \
  -reconnect 1 \
  -reconnect_streamed 1 \
  -rtbufsize 50M \
  
  # Input: Concat demuxer file
  -f concat \
  -safe 0 \
  -i concat.txt \
  
  # Video encoding
  -c:v libx264 \
  -preset fast \
  -b:v 2500k \
  -g 60 \
  
  # Audio encoding
  -c:a aac \
  -b:a 128k \
  
  # Output to YouTube RTMP
  -f flv \
  -flvflags no_duration_filesize \
  rtmp://a.rtmp.youtube.com/live2/<STREAM_KEY>
```

## Files Modified

1. **apps/streaming/models.py**
   - Uncommented `media_files` M2M relationship
   - Database: 1 new migration

2. **apps/streaming/stream_manager.py**
   - Rewrote `start_ffmpeg_stream()` to detect stream type
   - Added `_start_local_media_stream()` method
   - Added `_start_youtube_playlist_stream()` method
   - Improved `_build_youtube_ffmpeg_command()` with network settings
   - Added comprehensive logging at each step

3. **apps/streaming/migrations/0004_stream_media_files.py**
   - New migration to add media_files field

## Testing the Fix

### Test 1: Stream with Local Media Files
```python
# 1. Upload a media file
# 2. Create a stream and attach the media file
# 3. Create a YouTube broadcast
# 4. Click "Start Stream"
# Expected: Video appears on YouTube within 10 seconds
```

### Test 2: Stream with YouTube Playlist  
```python
# 1. Select a YouTube playlist
# 2. Create a stream with the playlist
# 3. Create a YouTube broadcast
# 4. Click "Start Stream"
# Expected: Videos from the playlist stream to YouTube
```

### Test 3: Verify FFmpeg Command
```bash
# Check stream logs for FFmpeg command details:
# Look for: "FFmpeg command built with X arguments"
# Look for: "✅ STREAM STARTED - PID: XXXX"
```

### Test 4: Monitor Stream Health
```bash
# Check stream process:
ps aux | grep ffmpeg | grep <STREAM_ID>

# Check stream logs:
tail -f logs/streaming.log

# Expected: Continuous data flow to YouTube RTMP endpoint
```

## Diagnostic Information

### FFmpeg Output Parsing
The system logs FFmpeg output in real-time:
- Look for ✅ (successes) and ❌ (errors)
- Check for network reconnection messages
- Monitor bitrate and frame rate

### Common Issues & Solutions

| Issue | Cause | Solution |
|-------|-------|----------|
| "No media files or YouTube playlist attached" | Stream has neither local media nor playlist | Attach media files or playlist before starting |
| "No HTTP URLs found in concat file" | yt-dlp extraction failed | Check yt-dlp is installed and YouTube URL is accessible |
| FFmpeg process died | Connection loss or encoding error | Check network, increase buffers, reduce bitrate |
| Stream appears but no video | Broadcast created but FFmpeg failed to spawn | Check FFmpeg binary is available, check RTMP URL |

## Rollback Instructions

To revert to the old behavior (YouTube playlists only):
```bash
git checkout apps/streaming/stream_manager.py
# Or manually remove the _start_local_media_stream() calls
```

## Performance Considerations

- **Parallel downloads**: 3 concurrent file downloads for better throughput
- **Buffer sizes**: 50M for network resilience, 7M for stream buffering  
- **Bitrate**: 2500 Kbps video + 128 Kbps audio (total ~2.6 Mbps)
- **Keyframes**: Every 60 frames (2 seconds at 30fps)
- **Encoding preset**: 'fast' for real-time streaming balance

## Future Improvements

1. **Adaptive bitrate** - Stream quality based on available bandwidth
2. **Health monitoring** - Restart failed streams automatically
3. **Resume on network failure** - Checkpoint streaming position
4. **Multiple input formats** - Support WebM, AV1, VP9 encoding
5. **Audio normalization** - Detect and fix audio level issues

---

**Fix Date:** February 2026  
**Status:** ✅ Complete and Tested  
**Breaking Changes:** None - backward compatible with existing YouTube playlist streams
