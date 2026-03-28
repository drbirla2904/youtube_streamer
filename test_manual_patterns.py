import os, django
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings'
django.setup()

from django.urls import path
from apps.streaming import views

# Try to manually build the urlpatterns exactly as urls.py does
try:
    urlpatterns = [
        path('connect/', views.connect_youtube, name='connect_youtube'),
        path('oauth2callback/', views.oauth_callback, name='oauth_callback'),
        path('streams/', views.stream_list, name='stream_list'),
        path('streams/create/', views.stream_create, name='stream_create'),
        path('streams/<uuid:stream_id>/', views.stream_detail, name='stream_detail'),
        path('streams/<uuid:stream_id>/start/', views.stream_start, name='stream_start'),
        path('streams/<uuid:stream_id>/stop/', views.stream_stop, name='stream_stop'),
        path('streams/<uuid:stream_id>/delete/', views.stream_delete, name='stream_delete'),
        path('streams/<uuid:stream_id>/status/', views.stream_status_api, name='stream_status_api'),
        path('streams/<uuid:stream_id>/thumbnail/', views.stream_update_thumbnail, name='stream_update_thumbnail'),
        path('streams/<uuid:stream_id>/download-playlist/', views.download_playlist_videos_view, name='download_playlist_videos'),
        path('media/', views.media_list_view, name='media_list'),
        path('media/upload/', views.media_upload_view, name='media_upload'),
        path('media/delete/<int:media_id>/', views.media_delete_view, name='media_delete'),
        path('media/reorder/', views.media_reorder_view, name='media_reorder'),
        path('streams/api/playlists/', views.user_playlists_api, name='user_playlists'),
        path('streams/api/playlist/<str:playlist_id>/videos/', views.playlist_videos_api, name='playlist_videos'),
        path('streams/playlist/<str:playlist_id>/fetch/', views.fetch_playlist_task, name='fetch_playlist_task'),
        path('cookies/upload/', views.upload_cookies_view, name='upload_cookies'),
        path('cookies/status/', views.cookies_status_api, name='cookies_status'),
    ]
    print(f"✓ Created {len(urlpatterns)} patterns (expected 20)")
    
    # Check if stream_update_thumbnail is there
    names = [p.name for p in urlpatterns if hasattr(p, 'name')]
    print(f"✓ stream_update_thumbnail in list: {'stream_update_thumbnail' in names}")
    
except Exception as e:
    print(f"✗ Error: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
