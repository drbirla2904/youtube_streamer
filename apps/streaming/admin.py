from django.contrib import admin
from .models import MediaFile, Stream, StreamLog


@admin.register(MediaFile)
class MediaFileAdmin(admin.ModelAdmin):
    list_display = ('title', 'user', 'media_type', 'file_size', 'created_at')
    list_filter = ('media_type', 'created_at')
    search_fields = ('title', 'user__username')
    readonly_fields = ('created_at',)


class StreamLogInline(admin.TabularInline):
    model = StreamLog
    extra = 0
    readonly_fields = ('level', 'message', 'created_at')
    can_delete = False


@admin.register(Stream)
class StreamAdmin(admin.ModelAdmin):
    list_display = ('title', 'user', 'youtube_account', 'status', 'loop_enabled', 'started_at', 'created_at')
    list_filter = ('status', 'loop_enabled', 'created_at')
    search_fields = ('title', 'user__username', 'youtube_account__channel_title')
    readonly_fields = ('id', 'created_at', 'updated_at', 'started_at', 'stopped_at')
    filter_horizontal = ()
    inlines = [StreamLogInline]
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('user', 'youtube_account', 'title', 'description')
        }),
        ('Media', {
            'fields': ('media_files', 'loop_enabled')
        }),
        ('Stream Details', {
            'fields': ('status', 'stream_key', 'broadcast_id', 'stream_url', 'process_id')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at', 'started_at', 'stopped_at')
        }),
        ('Error Information', {
            'fields': ('error_message',),
            'classes': ('collapse',)
        }),
    )


@admin.register(StreamLog)
class StreamLogAdmin(admin.ModelAdmin):
    list_display = ('stream', 'level', 'message', 'created_at')
    list_filter = ('level', 'created_at')
    search_fields = ('stream__title', 'message')
    readonly_fields = ('stream', 'level', 'message', 'created_at')
