#!/usr/bin/env python
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.streaming import urls as streaming_urls
from django.urls import get_resolver

print("=== Streaming URLs Analysis ===\n")

print(f"1. Checking urlpatterns in apps.streaming.urls:")
print(f"   Found {len(streaming_urls.urlpatterns)} patterns\n")

# List all patterns
for pattern in streaming_urls.urlpatterns:
    if hasattr(pattern, 'name') and pattern.name:
        print(f"   - {pattern.name}: {pattern.pattern}")

# Check if stream_update_thumbnail is in there
thumbnail_found = any(
    hasattr(p, 'name') and p.name == 'stream_update_thumbnail' 
    for p in streaming_urls.urlpatterns
)
print(f"\n2. stream_update_thumbnail in urlpatterns: {thumbnail_found}\n")

# Try to get resolver
resolver = get_resolver()
print(f"3. Global resolver URL names:")
all_names = []
for pattern in resolver.url_patterns:
    if hasattr(pattern, 'name') and pattern.name:
        all_names.append(pattern.name)
        print(f"   - {pattern.name}")

# Check included patterns
print(f"\n4. Checking reversing attempts:")
from django.urls import reverse, NoReverseMatch

names_to_try = ['stream_update_thumbnail', 'streaming:stream_update_thumbnail']
for name in names_to_try:
    try:
        import uuid
        url = reverse(name, kwargs={'stream_id': uuid.uuid4()})
        print(f"   ✓ {name}: {url}")
    except NoReverseMatch as e:
        print(f"   ✗ {name}: {e}")
