"""
Management command: diagnose_ytdlp

Diagnoses yt-dlp OAuth2 authentication issues and attempts fixes.
"""

import os
import sys
import subprocess
import json
from pathlib import Path
from django.core.management.base import BaseCommand
from django.conf import settings


class Command(BaseCommand):
    help = 'Diagnose and fix yt-dlp OAuth2 authentication issues'

    def handle(self, *args, **options):
        self.stdout.write(self.style.NOTICE(
            '\n══════════════════════════════════════════════\n'
            '  yt-dlp OAuth2 Diagnostic Tool\n'
            '══════════════════════════════════════════════\n'
        ))

        # 1. Check yt-dlp version
        self._check_ytdlp_version()

        # 2. Search for token file
        token_paths = self._search_token_file()

        if token_paths:
            self._handle_found_tokens(token_paths)
        else:
            self._suggest_reauth()

    def _check_ytdlp_version(self):
        """Check if yt-dlp is installed and get version."""
        try:
            r = subprocess.run(
                [sys.executable, '-m', 'yt_dlp', '--version'],
                capture_output=True, text=True, timeout=5
            )
            if r.returncode == 0:
                self.stdout.write(self.style.SUCCESS(
                    f'✓ yt-dlp installed: {r.stdout.strip()}'
                ))
            else:
                self.stdout.write(self.style.ERROR('✗ yt-dlp not working properly'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'✗ Error checking yt-dlp: {e}'))

    def _search_token_file(self):
        """Search for yt-dlp token file in common locations."""
        search_paths = [
            os.path.expanduser('~/.cache/yt-dlp/youtube-oauth2.json'),
            os.path.expanduser('~/.config/yt-dlp/youtube-oauth2.json'),
            os.path.expanduser('~/.local/share/yt-dlp/youtube-oauth2.json'),
            os.path.expanduser('~/AppData/Local/yt-dlp/youtube-oauth2.json'),
        ]

        found = []
        self.stdout.write('\nSearching for token file...')
        for path in search_paths:
            if os.path.exists(path):
                found.append(path)
                self.stdout.write(self.style.SUCCESS(f'✓ Found: {path}'))

        if not found:
            self.stdout.write(self.style.WARNING('✗ Token file not found in common locations'))

        return found

    def _handle_found_tokens(self, token_paths):
        """Handle case where tokens are found."""
        expected = getattr(
            settings,
            'YTDLP_OAUTH_TOKEN_DIR',
            os.path.expanduser('~/.cache/yt-dlp')
        )
        expected_file = os.path.join(expected, 'youtube-oauth2.json')

        if expected_file in token_paths:
            self.stdout.write(self.style.SUCCESS(
                '\n✅ Token is in the expected location and should work.\n'
                '   Restart your stream.'
            ))
            return

        # Token found but in different location
        main_token = token_paths[0]
        self.stdout.write(self.style.WARNING(
            f'\n⚠️  Token found at: {main_token}\n'
            f'    Expected at:   {expected_file}\n'
        ))

        # Copy or symlink the token
        Path(os.path.dirname(expected_file)).mkdir(parents=True, exist_ok=True)

        try:
            # Read the token
            with open(main_token, 'r') as f:
                token_data = json.load(f)

            # Write to expected location
            with open(expected_file, 'w') as f:
                json.dump(token_data, f)

            self.stdout.write(self.style.SUCCESS(
                f'\n✅ Token copied to expected location: {expected_file}\n'
                '   Your streams should now work.\n'
                '   If using scheduler, restart it:\n'
                f'   python manage.py setup_stream_scheduler'
            ))
        except Exception as e:
            self.stdout.write(self.style.ERROR(
                f'\n✗ Failed to copy token: {e}\n'
                f'   Manual fix needed. Please copy:\n'
                f'   cp {main_token} {expected_file}'
            ))

    def _suggest_reauth(self):
        """Suggest re-authentication if no token found."""
        self.stdout.write(self.style.ERROR(
            '\n✗ No OAuth2 token found.\n'
            '\nTo set up OAuth2 authentication:\n'
            '  1. Run: python manage.py setup_ytdlp_auth\n'
            '  2. Open: https://www.google.com/device\n'
            '  3. Enter the code shown\n'
            '  4. Sign in with a Google account\n'
            '  5. Grant permissions\n'
            '  6. Run this diagnostic again to verify\n'
        ))
