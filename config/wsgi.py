"""
WSGI config for youtube_streamer project.

This file exposes the WSGI callable as a module-level variable named ``application``.
It acts as a bridge between your Django application and any WSGI-compatible server
(Gunicorn, uWSGI, Apache mod_wsgi, etc.).

For more details, refer to the Django WSGI documentation:
https://docs.djangoproject.com/en/5.2/howto/deployment/wsgi/
"""

import os
import sys
from pathlib import Path
from django.core.wsgi import get_wsgi_application

# Compute base directory path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR))

# Set default settings module; override for production if needed
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

# Get WSGI application callable
application = get_wsgi_application()
