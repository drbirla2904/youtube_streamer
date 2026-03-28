import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

try:
    from apps.streaming.views import stream_update_thumbnail
    print("✓ SUCCESS: stream_update_thumbnail found!")
except ImportError as e:
    print(f"✗ FAILED: {e}")
