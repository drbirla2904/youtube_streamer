from django.contrib import admin
from .models import UserProfile, YouTubeAccount


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'phone', 'created_at')
    search_fields = ('user__username', 'user__email', 'phone')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(YouTubeAccount)
class YouTubeAccountAdmin(admin.ModelAdmin):
    list_display = ('channel_title', 'user', 'channel_id', 'is_active', 'created_at')
    list_filter = ('is_active', 'created_at')
    search_fields = ('channel_title', 'user__username', 'channel_id')
    readonly_fields = ('created_at', 'updated_at', 'token_expiry')
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('user', 'channel_id', 'channel_title', 'is_active')
        }),
        ('OAuth Tokens', {
            'fields': ('access_token', 'refresh_token', 'token_expiry'),
            'classes': ('collapse',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at')
        }),
    )
