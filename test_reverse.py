#!/usr/bin/env python
import os
import sys
import django

# Fresh setup
if 'config.settings' in sys.modules:
    del sys.modules['config.settings']
if 'django.urls' in sys.modules:
    del sys.modules['django.urls']

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.urls import reverse, NoReverseMatch
import uuid

test_uuid = uuid.uuid4()

# Test reversing
try:
    url = reverse('stream_update_thumbnail', kwargs={'stream_id': test_uuid})
    print(f"✓ SUCCESS: URL reversed to {url}")
except NoReverseMatch as e:
    print(f"✗ FAILED: {e}")
