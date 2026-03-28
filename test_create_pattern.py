import os, django
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings'
django.setup()

from django.urls import path
from apps.streaming import views

try:
    pattern = path('streams/<uuid:stream_id>/thumbnail/', views.stream_update_thumbnail, name='stream_update_thumbnail')
    print('✓ Pattern created successfully!')
    print(f'  Pattern: {pattern.pattern}')
    print(f'  Name: {pattern.name}')
except Exception as e:
    print(f'✗ Error: {type(e).__name__}: {e}')
    import traceback
    traceback.print_exc()
