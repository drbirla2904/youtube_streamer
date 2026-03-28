# Audio Download Truncation Fix

## Problem Identified

From your streaming logs, the actual bottleneck is **not FFmpeg/RTMP**, but **yt-dlp's YouTube audio download timing out prematurely**.

### Evidence from Logs

```
[https @ ...] Stream ends prematurely at 7585744, should be 9245632
[in#1/...] corrupt input packet in stream 0
[in#1/...] Error during demuxing: I/O error
```

**What this means:**
- Audio file size: **9,245,632 bytes** (expected)
- Downloaded: **7,585,744 bytes** (82% complete)  
- Missing: **1,659,888 bytes** (18% incomplete)

YouTube's server **closed the connection after 82%**, likely due to network timeout or rate limiting.

---

## Changes Made

### 1. Socket Timeout Increased
**File:** `apps/streaming/stream_manager.py` (line ~716)

```python
# BEFORE: '--socket-timeout', '30',  ← Too aggressive
# AFTER:  '--socket-timeout', '60',  ← More lenient for slow networks
```

**Reasoning:** 
- 9.2MB audio file download can take 4-5 minutes
- If any single packet takes >30s to arrive, timeout triggers
- Increased to 60s to handle network fluctuations

### 2. Fragment Retries Increased  
**File:** `apps/streaming/stream_manager.py` (line ~718)

```python
# BEFORE: '--fragment-retries', '10',
# AFTER:  '--fragment-retries', '15',  ← Better handling for dropped packets
```

**Note:** YouTube streams are fragmented; dropped packets cause full download restart if not recovered.

---

## How the Retry Logic Works

When incomplete stream detected (e.g., **Attempt 1 fails**):

```
⚠️  Incomplete stream for 'Corona hone...' (attempt 1/4)
   Downloaded 52353112 bytes but stream was incomplete
   Retrying with delay (15s)...
   ▶ [2/4] Corona hone...  ← Starts attempt 2 after 15s
```

**Retry Delays:**
- Attempt 1 → Fail? Wait **15s** then retry
- Attempt 2 → Fail? Wait **30s** then retry
- Attempt 3 → Fail? Wait **45s** then retry
- Attempt 4 → Fail? **Skip video, continue playlist**

This gives YouTube servers time to recover between retries.

---

## What's Still Unknown

1. **Why YouTube cuts off at 82%?**
   - Could be rate limiting (YouTube sees multiple attempts from same IP)
   - Could be ISP throttling
   - Could be network congestion
   - Could be residential IP restrictions

2. **Are you using a residential IP?**
   - YouTube prefers residential IPs over VPNs/proxies
   - If using a free/datacenter IP, YouTube may rate-limit downloads

---

## Next Steps to Improve Download Reliability

### Option 1: Use Better IP
```bash
# Check your IP type
curl ifconfig.me

# If using VPN or datacenter IP:
# 1. Try with residential IP/VPN
# 2. Or switch ISP if possible
```

### Option 2: Refresh Cookies
```bash
# Your current auth uses yt-cookies.txt
# Refresh it to get fresh session tokens

python manage.py setup_ytdlp_auth
```

### Option 3: Update yt-dlp (get latest fixes)
```bash
pip install -U yt-dlp
```

### Option 4: Stagger Attempts (manual)
Add additional delays between retry attempts:

```python
# File: apps/streaming/stream_manager.py (line ~821)
time.sleep(YTDLP_RETRY_BACKOFF * attempt * 2)  # 2x multiplier
```

This would give 30s, 60s, 90s, 120s between retries.

---

## Expected Behavior After Fix

**Before:** Audio truncates at 7.5MB → retry → truncates again → 300s timeout

**After:** 
- Download starts
- If timeout, waits 60s before timeout error (vs 30s before)
- Retry triggers with 15s delay
- Attempt 2 gets fresh connection
- Either completes OR fails again after 60s
- Process repeats up to 4 attempts

---

## FFmpeg Status

FFmpeg itself is working correctly:
- ✅ Starts without errors
- ✅ Accepts input from yt-dlp  
- ✅ Has RTMP timeout settings configured
- ✅ liveStream status would transition to "active" IF data flows smoothly

The `liveStream status: ready` in your logs is expected during incomplete streams because FFmpeg never gets complete input to start encoding frames.

---

## Testing the Fix

```bash
python manage.py runserver
# Create new stream
# Watch for incomplete stream messages
# Check if retry succeeds on attempt 2 or 3
```

If you still get incomplete streams after **2+ attempts**, the issue is likely:
1. Network/ISP throttling YouTube
2. Your IP is being rate-limited
3. Need to switch IPs or wait for YouTube cooldown
