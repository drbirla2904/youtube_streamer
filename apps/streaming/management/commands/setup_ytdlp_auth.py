"""
Management command: setup_ytdlp_auth

Instructions for exporting YouTube cookies to use with yt-dlp.

YouTube deprecated OAuth2 login for yt-dlp. Use cookies instead.
Chrome/Brave users: Install "Get cookies.txt LOCALLY" extension
Firefox users: Install "cookies.txt" extension

Place exported cookies at: yt-cookies.txt in the project root
"""

import os
import sys
from pathlib import Path
from django.core.management.base import BaseCommand
from django.conf import settings


def _get_cookies_file() -> str:
    """Get the path to the cookies file."""
    project_root = Path(__file__).resolve().parent.parent.parent.parent.parent
    cookies_file = getattr(settings, "YTDLP_COOKIES_FILE", "yt-cookies.txt")
    
    if os.path.isabs(cookies_file):
        return cookies_file
    return os.path.join(project_root, cookies_file)


YTDLP_COOKIES_FILE = _get_cookies_file()


class Command(BaseCommand):
    help = 'Guide for setting up YouTube cookies for yt-dlp authentication.'

    def handle(self, *args, **options):
        self.stdout.write(self.style.NOTICE(
            '\n══════════════════════════════════════════════════════════════\n'
            '  YouTube Cookies Setup for yt-dlp\n'
            '══════════════════════════════════════════════════════════════\n'
        ))

        self.stdout.write(
            'YouTube deprecated OAuth2 login for yt-dlp.\n'
            'You now need to export cookies from your browser.\n'
        )

        # Check if cookies file exists
        if os.path.exists(YTDLP_COOKIES_FILE):
            file_size = os.path.getsize(YTDLP_COOKIES_FILE)
            self.stdout.write(self.style.SUCCESS(
                f'\n✅ Cookies file found: {YTDLP_COOKIES_FILE}\n'
                f'   Size: {file_size} bytes\n'
                f'   Streams should work automatically!\n'
            ))
            return

        # Guide user to export cookies
        self.stdout.write(self.style.WARNING(
            '\n⚠️  No cookies file found at:\n'
            f'    {YTDLP_COOKIES_FILE}\n'
        ))

        self.stdout.write(self.style.NOTICE(
            '\n──────────────────────────────────────────────────────────────\n'
            'STEP 1: Install a Cookie Export Extension\n'
            '──────────────────────────────────────────────────────────────\n'
        ))

        self.stdout.write(
            'Chrome/Brave/Edge:\n'
            '  🔗 Install: "Get cookies.txt LOCALLY"\n'
            '     https://chrome.google.com/webstore/detail/get-cookies-txt-locally/cclelndcbgoacstlcjggajpnjccfbgkj\n'
        )

        self.stdout.write(
            '\nFirefox:\n'
            '  🔗 Install: "cookies.txt"\n'
            '     https://addons.mozilla.org/firefox/addon/cookies-txt/\n'
        )

        self.stdout.write(self.style.NOTICE(
            '\n──────────────────────────────────────────────────────────────\n'
            'STEP 2: Export Cookies\n'
            '──────────────────────────────────────────────────────────────\n'
        ))

        self.stdout.write(
            '1. Sign into YouTube (youtube.com) in your browser\n'
            '2. Right-click anywhere on the page\n'
            '3. Select "Export cookies as .txt (Netscape format)"\n'
            '4. Save the file\n'
        )

        self.stdout.write(self.style.NOTICE(
            '\n──────────────────────────────────────────────────────────────\n'
            'STEP 3: Place Cookies File\n'
            '──────────────────────────────────────────────────────────────\n'
        ))

        self.stdout.write(
            f'Copy the exported cookies.txt file to:\n'
            f'  {YTDLP_COOKIES_FILE}\n'
            '\nOr configure the path in .env:\n'
            f'  YTDLP_COOKIES_FILE=./path/to/cookies.txt\n'
        )

        self.stdout.write(self.style.SUCCESS(
            '\n✅ Once done, your streams will work automatically!\n'
            '   The cookies will be used for downloading YouTube videos.\n'
            '──────────────────────────────────────────────────────────────\n'
        ))

    def _token_works(self) -> bool:
        """Test if the stored OAuth2 token is still valid."""
        try:
            r = subprocess.run(
                [sys.executable, '-m', 'yt_dlp',
                 '--username', 'oauth2', '--password', '',
                 '-F', '--no-playlist', '--quiet', TEST_URL],
                capture_output=True, text=True, timeout=30
            )
            # Valid if yt-dlp exits 0 OR exits non-zero but NOT due to auth
            return 'Sign in' not in r.stderr and 'oauth2' not in r.stderr.lower()
        except Exception:
            return False

    def _find_token_file(self) -> str:
        """Check the expected token file location."""
        if os.path.exists(YTDLP_TOKEN_FILE):
            return YTDLP_TOKEN_FILE
        return ''

    def _search_for_token(self) -> str:
        """Search common locations for the token file."""
        search_dirs = [
            os.path.expanduser('~/.cache/yt-dlp'),
            os.path.expanduser('~/.yt-dlp'),
            '/tmp',
            os.path.expanduser('~/'),
        ]
        for d in search_dirs:
            candidate = os.path.join(d, 'youtube-oauth2.json')
            if os.path.exists(candidate):
                return candidate

        # Try find command as last resort
        try:
            r = subprocess.run(
                ['find', os.path.expanduser('~'), '-name', 'youtube-oauth2.json',
                 '-maxdepth', '5'],
                capture_output=True, text=True, timeout=10
            )
            if r.stdout.strip():
                return r.stdout.strip().split('\n')[0]
        except Exception:
            pass
        return ''