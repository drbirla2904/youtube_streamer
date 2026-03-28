import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.streaming.urls import urlpatterns
print(f"Total URL patterns: {len(urlpatterns)}")

for pattern in urlpatterns:
    if hasattr(pattern, 'name'):
        print(f"  - {pattern.name if pattern.name else '(unnamed)'}")

# Check if line 23 pattern exists
thumbnail_patterns = [p for p in urlpatterns if hasattr(p, 'name') and p.name == 'stream_update_thumbnail']
print(f"\nstream_update_thumbnail patterns found: {len(thumbnail_patterns)}")
if thumbnail_patterns:
    p = thumbnail_patterns[0]
    print(f"Pattern: {p.pattern}")
    print(f"Callback: {p.callback}")
