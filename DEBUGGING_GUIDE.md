# Post-Deployment Debugging Guide

## 🔴 Issue: "Download button not appearing"

### Symptoms
- Stream detail page loads but no "Download Playlist Videos" button
- User doesn't see download option

### Root Causes & Fixes

| Cause | Check | Fix |
|-------|-------|-----|
| URL routing not configured | `grep 'download-playlist' apps/streaming/urls.py` | Ensure path exists in urlpatterns |
| View not imported | `grep 'download_playlist_videos_view' apps/streaming/views.py` | Ensure view is in urls.py imports |
| Template not updated | Check `templates/streaming/stream_detail.html` | Add download button to template |
| Permission issue | Check user.is_authenticated | Verify @login_required decorator |
| Stream has no playlist | Check stream.playlist_videos | Only show button if playlist_videos is populated |

### Quick Debug
```bash
# Check URL routing
cd /workspaces/youtube_streamer
python manage.py shell
>>> from django.urls import reverse
>>> reverse('download_playlist_videos', kwargs={'stream_id': 'your-uuid'})
# Should return URL like /streams/abc-def/download-playlist/

# Check view exists
>>> from apps.streaming.views import download_playlist_videos_view
>>> print(download_playlist_videos_view)
# Should print function object, not error
```

---

## 🔴 Issue: "Download starts but nothing happens"

### Symptoms
- Click "Download Playlist Videos"
- Redirected with success message
- But no videos appear in media_files
- No logs in StreamLog

### Root Causes & Fixes

| Cause | Check | Fix |
|-------|-------|-----|
| Celery not running | `celery -A config status` | Start worker: `celery -A config worker` |
| Redis not running | `redis-cli ping` | Start Redis or check connection |
| Task not registered | `python manage.py shell` → `from apps.streaming.tasks import download_playlist_videos_async` → `print(download_playlist_videos_async)` | Verify task imports in settings.py |
| yt-dlp not installed | `which yt-dlp` | Install: `pip install yt-dlp` |
| FFprobe not installed | `which ffprobe` | Install: `apt-get install ffmpeg` |
| Wrong playlist_videos format | Check database: `Stream.objects.filter(id='').values('playlist_videos')` | Ensure playlist_videos is valid JSON |
| Storage not configured | Check S3 credentials or local path | Verify MEDIA_ROOT and storage backend |

### Quick Debug
```bash
# Check Celery
celery -A config inspect active  # Should show worker

# Check Redis
redis-cli ping  # Should return PONG

# Check task was queued
redis-cli LRANGE celery 0 10  # Should show pending tasks

# Check Celery logs
tail -f /var/log/celery/worker.log  # Or wherever you log Celery

# Test yt-dlp manually
yt-dlp -f best[ext=mp4] --dump-json 'https://www.youtube.com/watch?v=dQw4w9WgXcQ' | jq '.title'
```

### Check StreamLog
```python
from apps.streaming.models import Stream, StreamLog

stream = Stream.objects.get(id='your-uuid')
logs = StreamLog.objects.filter(stream=stream).order_by('-created_at')[:20]

for log in logs:
    print(f"{log.created_at} [{log.level}] {log.message}")
```

---

## 🔴 Issue: "Download fails with 'No such file' error"

### Symptoms
- Download starts
- Fails with error about missing file
- StreamLog shows: `FileNotFoundError: ...`

### Root Causes & Fixes

| Cause | Check | Fix |
|-------|-------|-----|
| /var/tmp not writable | `ls -la /var/tmp` | Fix permissions: `chmod 777 /var/tmp` |
| Storage path invalid | Check MEDIA_ROOT in settings.py | Ensure path exists: `mkdir -p /path` |
| Disk full | `df -h` | Free up space or expand storage |
| Downloaded file corrupted | Check download manually | Retry download or check YouTube video quality |
| Temp file deleted during download | Check OS cleanup | Increase temp file retention or use dedicated folder |

### Quick Debug
```bash
# Test temp file access
touch /var/tmp/test_file && rm /var/tmp/test_file
# Should succeed without permission errors

# Test storage access  
python manage.py shell
>>> from django.conf import settings
>>> import os
>>> os.makedirs(settings.MEDIA_ROOT, exist_ok=True)
>>> with open(os.path.join(settings.MEDIA_ROOT, 'test.txt'), 'w') as f:
...     f.write('test')
# Should complete without error

# Check disk space
df -h /path/to/media  # Ensure >50GB free
```

---

## 🔴 Issue: "yt-dlp fails to download"

### Symptoms
- StreamLog shows: `yt-dlp: error: ...`
- Or: `Permission denied` when running yt-dlp
- Or: `HTTP 403 Forbidden` from YouTube

### Root Causes & Fixes

| Cause | Check | Fix |
|-------|-------|-----|
| YouTube blocked IP | Try from browser first | Use VPN or proxy if blocked |
| Playlist is private | Check YouTube account | Make playlist public or unlisted |
| Video is private/deleted | Check video URL directly | Skip deleted videos |
| yt-dlp outdated | `yt-dlp --version` | Update: `pip install --upgrade yt-dlp` |
| Format not available | Check available formats | Use different format spec |
| Network timeout | Check internet connection | Increase timeout in download function |
| Rate limited | Check YouTube response | Add delays between downloads |

### Quick Debug
```bash
# Test yt-dlp directly
yt-dlp -f best[ext=mp4] 'https://www.youtube.com/playlist?list=PLxxxxx' \
  --dump-json | jq '.entries | length'
# Should return number of videos

# Test single video
yt-dlp -f best[ext=mp4] --simulate \
  'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
# Should show video info without downloading

# Check yt-dlp version
yt-dlp --version  # Ensure >2026.01.00
```

### Fix in Code
```python
# In stream_manager.py, increase timeout and add retry
import subprocess

for attempt in range(3):  # Retry 3 times
    try:
        result = subprocess.run(
            ['yt-dlp', '-f', 'best[ext=mp4]', url],
            timeout=900,  # 15 minutes
            capture_output=True
        )
        if result.returncode == 0:
            break
    except subprocess.TimeoutExpired:
        if attempt < 2:
            continue
        raise
```

---

## 🔴 Issue: "Download takes too long (>2 hours)"

### Symptoms
- Download for 50 videos takes 2+ hours
- Users waiting but nothing happening
- Celery task timeout approaching

### Root Causes & Analysis

| Issue | Expected | Actual | Fix |
|-------|----------|--------|-----|
| Network slow | 5 min/video | 15+ min/video | Check bandwidth: `iperf3` |
| YouTube throttling | 5 Mbps steady | Variable 1-10 Mbps | Add delays, rotate proxies |
| Storage write slow | 50 MB/s | <10 MB/s | Check IOPS: `fio` |
| Single video too large | <500MB | >1GB | Add quality filter |
| Sequential download | Could be parallel | Downloads one at a time | Implement parallel workers |

### Performance Optimization
```python
# Option 1: Limit max videos
download_playlist_videos(stream, max_videos=10)  # Instead of 50

# Option 2: Lower quality
# Change in stream_manager.py:
# FROM: '-f', 'best[ext=mp4]'
# TO:   '-f', '18'  # 360p quality

# Option 3: Parallel downloads
from concurrent.futures import ThreadPoolExecutor

with ThreadPoolExecutor(max_workers=5) as executor:
    futures = [executor.submit(download_video, v) for v in videos]
    results = [f.result() for f in futures]

# Option 4: Increase task timeout
# In tasks.py:
@shared_task(time_limit=7200)  # 2 hours instead of 1
def download_playlist_videos_async(...):
```

### Quick Debug
```bash
# Check network bandwidth
iperf3 -c youtube.com  # Or speedtest

# Monitor download in progress
python manage.py shell
>>> from apps.streaming.models import MediaFile, Stream
>>> stream = Stream.objects.get(id='uuid')
>>> stream.media_files.count()
# Run this periodically to see progress

# Check Celery task progress
celery -A config events  # Real-time monitoring
```

---

## 🔴 Issue: "FFmpeg stream shows no video"

### Symptoms
- Download completes successfully
- Stream starts (status='running')
- But YouTube shows black/blank video
- No video appears, only audio or nothing

### Root Causes & Fixes

| Cause | Check | Fix |
|-------|-------|-----|
| Concat file malformed | Check /tmp/concat_*.txt | Verify paths exist in concat file |
| H.264 encoding issue | Check FFmpeg command | Ensure `-c:v libx264` specified |
| FFmpeg crash | Check process alive: `ps aux \| grep ffmpeg` | Check FFmpeg logs for errors |
| RTMP connection failed | Check YouTube key/URL valid | Verify key not revoked on YouTube |
| Video file corrupted | Download manually and check | Re-download specific video |

### Quick Debug
```bash
# Check concat file
ls -la /tmp/concat_*.txt
cat /tmp/concat_*.txt
# Should show file:// entries that actually exist

# Check FFmpeg process
ps aux | grep ffmpeg
# Should show process with 59+ arguments

# Monitor FFmpeg with logging
ffmpeg -i concat_file.txt ... -loglevel debug output.flv 2>&1 | tee ffmpeg.log

# Check RTMP key works
ffmpeg -f lavfi -i testsrc=s=1280x720:d=5 -pix_fmt yuv420p \
  -f lavfi -i sine=frequency=1000:duration=5 \
  -c:v libx264 -c:a aac \
  -f flv "rtmp://a.rtmp.youtube.com/live2/YOUR_KEY"
```

### Manual Test
```python
from apps.streaming.stream_manager import StreamManager
from apps.streaming.models import Stream

stream = Stream.objects.get(id='uuid')
manager = StreamManager(stream)

# Check stream type detection
print(f"Has local media: {stream.media_files.exists()}")
print(f"Media files count: {stream.media_files.count()}")
print(f"Has playlist: {bool(stream.playlist_videos)}")

# Check FFmpeg command
if stream.media_files.exists():
    cmd = manager._build_youtube_ffmpeg_command('/tmp/concat.txt')
    print(f"FFmpeg command ({len(cmd)} args):")
    for i, arg in enumerate(cmd):
        print(f"  [{i:2d}] {arg}")
```

---

## 🔴 Issue: "Memory or CPU spiking during download"

### Symptoms
- Server CPU at 100% during download
- Memory usage growing continuously
- System becomes unresponsive

### Root Causes & Fixes

| Cause | Check | Fix |
|-------|-------|-----|
| Downloading too many videos | Check max_videos parameter | Reduce default: `max_videos=10` instead of 50 |
| Large videos not compressed | Check video sizes | Use quality filter: `-f 18` (360p instead of 4K) |
| yt-dlp not closing process | Check processes: `ps aux \| grep yt-dlp` | Ensure subprocess returns early |
| Media files not deleted after upload | Check /var/tmp size | Delete temp files after upload to storage |
| FFprobe hanging on large files | Check ffprobe timeout | Add timeout to FFprobe command |

### Optimization Code
```python
# Limit quality to reduce file size
def download_youtube_playlist_videos(stream, max_videos=10):  # Reduced from 50
    # Use 360p instead of best quality
    format_spec = '18'  # 360p MP4 (lightweight)
    
    # Add timeouts
    import signal
    
    def timeout_handler(signum, frame):
        raise TimeoutError("FFprobe timeout")
    
    # Clean up temp files aggressively
    import shutil
    for f in glob.glob('/var/tmp/video_*.mp4'):
        try:
            shutil.rmtree(f)
        except:
            pass
    
    # Monitor memory
    import psutil
    if psutil.virtual_memory().percent > 80:
        logger.warning("Memory usage high, pausing downloads")
        time.sleep(60)
```

### Quick Debug
```bash
# Monitor system during download
watch -n 1 'free -h; ps aux | grep -E "(yt-dlp|ffmpeg|ffprobe)" | head -5'

# Check process file descriptor limits
ps aux | grep yt-dlp | awk '{print $2}' | xargs -I{} lsof -p {} | wc -l

# Check temp folder size
du -sh /var/tmp/

# Kill stuck processes
pkill -9 yt-dlp
pkill -9 ffprobe
```

---

## 📋 Diagnostic Checklist

When something fails, run through this:

```bash
# 1. Check services
redis-cli ping                          # Should: PONG
celery -A config inspect active         # Should: show worker
ps aux | grep "gunicorn|uwsgi"          # Should: Django process

# 2. Check dependencies  
which ffmpeg ffprobe yt-dlp             # All should exist
youtube-dl --version 2>/dev/null || yt-dlp --version  # Version info

# 3. Check permissions
touch /var/tmp/test && rm /var/tmp/test # Should: no errors
ls -la /workspaces/youtube_streamer/media/  # Should: readable

# 4. Check database
python manage.py shell
>>> from apps.streaming.models import Stream, StreamLog, MediaFile
>>> Stream.objects.count()              # Should: >0
>>> MediaFile.objects.count()           # Should: >0

# 5. Check configuration
python manage.py shell
>>> from django.conf import settings
>>> print(f"DEBUG: {settings.DEBUG}")
>>> print(f"CELERY_BROKER: {settings.CELERY_BROKER_URL}")
>>> print(f"MEDIA_ROOT: {settings.MEDIA_ROOT}")

# 6. Check logs
tail -f /var/log/django/error.log       # Django errors
tail -f /var/log/celery/worker.log      # Celery errors
```

---

## 🆘 Emergency Recovery

If everything fails:

```bash
# 1. Stop all processes
celery -A config control shutdown
pkill -9 ffmpeg yt-dlp ffprobe

# 2. Clear stuck downloads
rm -f /var/tmp/video_*.mp4
rm -f /tmp/concat_*.txt

# 3. Reset stream status
python manage.py shell
>>> from apps.streaming.models import Stream
>>> Stream.objects.filter(status='running').update(status='idle')

# 4. Clear task queue
redis-cli FLUSHDB

# 5. Restart everything
service redis-server restart
celery -A config worker restart
service django-gunicorn restart

# 6. Monitor recovery
celery -A config events

# 7. Retry download
# Log in and click download button again
```

---

## 📞 Escalation Path

**Still broken after this guide?**

1. **Collect logs** (in order):
   ```bash
   tail -100 /var/log/celery/worker.log > celery_log.txt
   tail -100 /var/log/django/error.log > django_log.txt
   sqlite3 db.sqlite3 "SELECT * FROM streaming_streamlog WHERE stream_id='UUID' LIMIT 50" > stream_log.csv
   ```

2. **Run diagnostics**:
   ```bash
   python manage.py shell
   >>> from apps.streaming.models import Stream, MediaFile
   >>> s = Stream.objects.get(id='UUID')
   >>> print(f"Status: {s.status}")
   >>> print(f"Playlist: {bool(s.playlist_videos)}")
   >>> print(f"Media files: {s.media_files.count()}")
   ```

3. **Document**:
   - What exactly happened?
   - What's in the logs?
   - What's the stream state?
   - What commands fail?

4. **Contact developer** with:
   - logs (above)
   - stream ID
   - exact error message
   - reproduction steps

