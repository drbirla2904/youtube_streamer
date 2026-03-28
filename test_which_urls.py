import os, django, inspect
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings'
django.setup()

from apps.streaming import urls as streaming_urls

print(f"URLs module file: {inspect.getfile(streaming_urls)}")
print(f"Has stream_update_thumbnail: {hasattr(streaming_urls, 'stream_update_thumbnail')}")
