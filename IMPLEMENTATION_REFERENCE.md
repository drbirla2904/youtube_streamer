# Implementation Reference: Download → Store → Stream via FFmpeg

## 🎬 Complete Flow Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│ User clicks "Start Stream" with YouTube Playlist                 │
└────────────────────┬────────────────────────────────────────────┘
                     │
        ┌────────────▼───────────────┐
        │ start_ffmpeg_stream()       │
        │ (stream_manager.py:686)    │
        └────────┬───────────────────┘
                 │
        ┌────────▼──────────────────────────────────┐
        │ Detect Stream Type:                        │
        │ - has_local_media? (YES/NO)              │
        │ - has_youtube_playlist? (YES/NO)         │
        └────────┬───────────────┬──────────────────┘
                 │               │
         ┌───────▼──────┐  ┌─────▼──────────────────┐
         │ LOCAL MEDIA  │  │ YOUTUBE PLAYLIST       │
         │ PATH         │  │ _start_youtube_        │
         │              │  │ playlist_stream()      │
         └──────────────┘  │ (stream_manager.py:851)│
                           └────────┬───────────────┘
                                    │
                           ┌────────▼────────────────────┐
                           │ Check existing downloads:   │
                           │ stream.media_files.exists() │
                           └────────┬────────┬───────────┘
                                    │        │
                               YES  │        │  NO
                           ┌────────▼──┐  ┌──▼──────────────────┐
                           │ Use Local │  │ Start Download:      │
                           │ Media     │  │ download_youtube_    │
                           │ Stream    │  │ playlist_videos()    │
                           └───────────┘  │ (stream_manager.py)  │
                                          └────────┬─────────────┘
                                                   │
                          ┌────────────────────────▼───────────────┐
                          │ For each video in playlist (max 50):    │
                          └────────────────────┬───────────────────┘
                                               │
                          ┌────────────────────▼───────────────────┐
                          │ 1. yt-dlp: Extract direct video URL    │
                          │    $ yt-dlp -f best[ext=mp4] -g VIDEO  │
                          └────────────────────┬───────────────────┘
                                               │
                          ┌────────────────────▼───────────────────┐
                          │ 2. Download video file to /var/tmp/    │
                          │    $ yt-dlp -o video_{id}.mp4 VIDEO    │
                          └────────────────────┬───────────────────┘
                                               │
                          ┌────────────────────▼───────────────────┐
                          │ 3. FFprobe: Get duration                │
                          │    $ ffprobe -v error video.mp4         │
                          └────────────────────┬───────────────────┘
                                               │
                          ┌────────────────────▼───────────────────┐
                          │ 4. Create MediaFile object in database  │
                          │    - title: from YouTube                │
                          │    - duration: from FFprobe             │
                          │    - file_size: actual file size        │
                          │    - file: upload to storage (S3/disk)  │
                          └────────────────────┬───────────────────┘
                                               │
                          ┌────────────────────▼───────────────────┐
                          │ 5. Link to stream:                      │
                          │    stream.media_files.add(media_file)   │
                          └────────────────────┬───────────────────┘
                                               │
                          ┌────────────────────▼───────────────────┐
                          │ 6. Loop next video...                   │
                          │    (3 parallel workers)                 │
                          └────────────────────┬───────────────────┘
                                               │
                                ┌──────────────▼────────────────┐
                                │ All downloads complete!        │
                                │ stream.media_files.count() > 0 │
                                └──────────────┬────────────────┘
                                               │
                        ┌──────────────────────▼─────────────────┐
                        │ Switch to local media stream mode:     │
                        │ _start_local_media_stream()            │
                        │ (stream_manager.py:714)                │
                        └──────────────────────┬─────────────────┘
                                               │
                        ┌──────────────────────▼──────────────────┐
                        │ 1. Download media files from storage    │
                        │ 2. Create FFmpeg concat file            │
                        │ 3. Build FFmpeg command                 │
                        │ 4. Spawn FFmpeg process                 │
                        │ 5. FFmpeg uploads to YouTube RTMP       │
                        │ 6. Update stream.status = 'running'     │
                        └──────────────────────┬──────────────────┘
                                               │
                        ┌──────────────────────▼──────────────────┐
                        │ ✅ VIDEO APPEARS ON YOUTUBE              │
                        │    (10-15 seconds after stream start)   │
                        └───────────────────────────────────────────┘
```

---

## 🔄 Method Flow Chart

### Main Entry Point
```python
class StreamManager:
    def start_ffmpeg_stream(self):
        """Main entry - determines which path to take"""
        has_local = stream.media_files.exists()
        has_playlist = bool(stream.playlist_videos)
        
        if has_local:
            return self._start_local_media_stream()  # Existing path
        elif has_playlist:
            return self._start_youtube_playlist_stream()  # NEW!
        else:
            raise Exception("No media or playlist")
```

### YouTube Playlist Streaming Path (NEW)
```python
def _start_youtube_playlist_stream(self):
    """NEW: Download first, then stream"""
    
    # Check if already downloaded
    if self.stream.media_files.exists():
        # Reuse existing downloads!
        return self._start_local_media_stream()
    
    # Download for first time
    try:
        media_files = download_youtube_playlist_videos(
            self.stream, 
            max_videos=50
        )
        if media_files:
            # Now stream from downloaded files
            return self._start_local_media_stream()
    except Exception as e:
        logger.warning("Download failed, trying direct URL...")
        # Fallback to URL streaming
        return self._start_youtube_url_stream()
```

---

## 📝 Code Reference

### Function Signature
```python
def download_youtube_playlist_videos(stream, max_videos=50) -> List[MediaFile]:
    """
    Download all videos from YouTube playlist and store as MediaFile objects
    
    Parameters:
        stream: Stream object with populated playlist_videos
        max_videos: Limit to this many videos (default 50)
    
    Returns:
        List of created MediaFile objects
    
    Process:
        1. Get playlist metadata from stream.playlist_videos
        2. For each video (up to max_videos):
           a. Use yt-dlp to download MP4 file
           b. Use FFprobe to extract duration
           c. Create MediaFile database entry
           d. Upload file to configured storage
           e. Link to stream via M2M relationship
        3. Update stream.playlist_videos with status
        4. Return list of created MediaFile objects
    
    Exceptions:
        - Exception: If playlist_videos is empty/invalid
        - Exception: If no videos can be downloaded
    """
```

### Processing Steps

```python
# Step 1: Get playlist data
playlist_data = stream.playlist_videos  # JSONField
videos = playlist_data[0]['videos']      # Get video list

# Step 2: For each video
for video in videos[:max_videos]:
    
    # Step 2a: Download with yt-dlp
    cmd = [
        'yt-dlp',
        '-f', 'best[ext=mp4]',  # Get best MP4
        '-o', f'video_{video_id}.mp4',
        'https://www.youtube.com/watch?v=' + video_id
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=600)
    
    # Step 2b: Get duration with FFprobe
    duration = get_video_duration(file_path)
    
    # Step 2c: Create MediaFile
    media_file = MediaFile.objects.create(
        user=stream.user,
        title=video['title'],
        media_type='video',
        mime_type='video/mp4',
        duration=duration,
        file_size=os.path.getsize(file_path),
        sequence=idx
    )
    
    # Step 2d: Upload to storage
    with open(file_path, 'rb') as f:
        media_file.file.save(
            f'youtube_{video_id}.mp4',
            ContentFile(f.read()),
            save=True
        )
    
    # Step 2e: Link to stream
    stream.media_files.add(media_file)
    
    # Clean up temp file
    os.remove(file_path)
```

---

## 🔗 Integration Points

### Updated Methods

```python
# StreamManager.__init__ - No changes needed

# StreamManager.start_ffmpeg_stream() - UPDATED
def start_ffmpeg_stream(self):
    # NEW: Dual routing logic
    if stream.media_files.exists():
        return self._start_local_media_stream()
    elif stream.playlist_videos:
        return self._start_youtube_playlist_stream()  # NEW!
    else:
        raise Exception(...)

# StreamManager._start_youtube_playlist_stream() - NEW
def _start_youtube_playlist_stream(self):
    # NEW: Download-first approach
    if not stream.media_files.exists():
        media_files = download_youtube_playlist_videos(stream)
    return self._start_local_media_stream()

# StreamManager.download_playlist_videos() - NEW
def download_playlist_videos(self, max_videos=50):
    media_files = download_youtube_playlist_videos(self.stream, max_videos)
    return len(media_files)
```

### New View

```python
# views.py - NEW VIEW
@login_required
@require_POST
def download_playlist_videos_view(request, stream_id):
    stream = get_object_or_404(Stream, id=stream_id, user=request.user)
    
    # Validations
    if stream.status == 'running':
        messages.error(request, 'Stop stream first')
        return redirect('stream_detail', stream_id=stream.id)
    
    # Start async download
    task = download_playlist_videos_async.delay(
        str(stream.id),
        max_videos=int(request.POST.get('max_videos', 50))
    )
    
    messages.success(request, f'Download started (Task {task.id})')
    return redirect('stream_detail', stream_id=stream.id)
```

### New Celery Task

```python
# tasks.py - NEW TASK
@shared_task(time_limit=3600, soft_time_limit=3000)
def download_playlist_videos_async(stream_id, max_videos=50):
    """Background task for downloading"""
    stream = Stream.objects.get(id=stream_id)
    manager = StreamManager(stream)
    
    try:
        count = manager.download_playlist_videos(max_videos=max_videos)
        StreamLog.objects.create(
            stream=stream,
            level='INFO',
            message=f'Downloaded {count} videos'
        )
        return f"Downloaded {count} videos"
    except Exception as e:
        stream.status = 'error'
        stream.error_message = str(e)
        stream.save()
        raise
```

### New URL Route

```python
# urls.py - NEW ROUTE
path('streams/<uuid:stream_id>/download-playlist/', 
     views.download_playlist_videos_view, 
     name='download_playlist_videos')
```

---

## 🧪 Testing Entry Points

```python
# Test basic function
from apps.streaming.stream_manager import download_youtube_playlist_videos

stream = Stream.objects.get(id='uuid')
media_files = download_youtube_playlist_videos(stream, max_videos=5)
print(f"Downloaded {len(media_files)} videos")

# Test StreamManager method
manager = StreamManager(stream)
count = manager.download_playlist_videos(max_videos=10)
print(f"Manager reports: {count} videos")

# Test Celery task
from apps.streaming.tasks import download_playlist_videos_async

task = download_playlist_videos_async.delay(str(stream.id), max_videos=20)
print(f"Task {task.id} started")

# Check result
result = task.get(timeout=30)  # Wait up to 30 seconds
print(f"Result: {result}")
```

---

## 📊 State Transitions

```
Stream States During Download:

idle
  ↓ [Click Download button]
  ├─→ Task created (async download starts)
  ├─→ Logs recorded to stream.logs
  │
  └─→ Check stream.media_files.count()
      ├─→ 0: Still downloading
      ├─→ N: Complete (can start stream)
      └─→ With error_message: Failed

Stream.playlist_videos changes:
{old format}
  ↓ [After download completes]
{new format with 'status': 'downloaded', 'media_file_ids': [...]}
```

---

## ⚡ Performance Characteristics

```
Download Performance:
├─ Per video: 1-10 minutes (depends on size)
├─ Parallel workers: 3 concurrent
├─ Typical 50 videos: 40-80 minutes
├─ Bandwidth: 3-10 Mbps (YouTube dependent)
└─ Bottleneck: YouTube throttling vs network

Storage Performance:
├─ Read speed: 500+ MB/s (SSD)
├─ Write speed: 100-500 MB/s (storage type)
├─ Upload to storage: Concurrent with downloads
└─ No impact on streaming performance

FFmpeg Performance:
├─ Read from storage: 50-500 Mbps
├─ Encoding: Real-time (depends on CPU)
├─ Output: 2.5 Mbps to YouTube RTMP
└─ Result: Smooth streaming to viewers
```

---

## 🚀 Deployment Checklist

- [ ] All code deployed to production
- [ ] Celery worker running: `celery -A config worker`
- [ ] Redis/Broker running: `redis-cli ping`
- [ ] yt-dlp installed: `which yt-dlp`
- [ ] FFmpeg installed:  `which ffmpeg`
- [ ] Database migration done: Already applied ✅
- [ ] Tests passing: `python test_playlist_download.py` ✅
- [ ] View tests: Click download button on stream
- [ ] Async tests: Check Celery task queue
- [ ] Integration test: Full download + stream cycle

---

This reference provides a complete technical overview of the YouTube playlist download & storage streaming implementation.
