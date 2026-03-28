#!/usr/bin/env python
import os
import sys
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.urls import reverse, get_resolver
import uuid

# Test if stream_update_thumbnail URL can be reversed
try:
    test_uuid = uuid.uuid4()
    url = reverse('streaming:stream_update_thumbnail', kwargs={'stream_id': test_uuid})
    print(f"SUCCESS! URL reversed: {url}")
except Exception as e:
    print(f"ERROR: {e}")
    print("\nAvailable URL patterns:")
    resolver = get_resolver()
    for pattern in resolver.url_patterns:
        if hasattr(pattern, 'name') and pattern.name:
            print(f"  - {pattern.name}: {pattern.pattern}")
        elif hasattr(pattern, 'pattern'):
            print(f"  - (namespace): {pattern.pattern}")
