"""
Migration: add cookies_txt field to YouTubeAccount.

This stores the Netscape-format cookies.txt exported from a browser
so yt-dlp can authenticate as the channel owner and bypass YouTube's
bot detection when streaming playlist videos.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='youtubeaccount',
            name='cookies_txt',
            field=models.TextField(
                blank=True,
                default='',
                help_text=(
                    'Paste your YouTube cookies in Netscape format here. '
                    'Export from browser using the "Get cookies.txt LOCALLY" '
                    'Chrome extension or "cookies.txt" Firefox extension. '
                    'Required for yt-dlp playlist streaming.'
                ),
            ),
        ),
    ]
