import os
from pathlib import Path
from decouple import config



BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config('SECRET_KEY')
DEBUG = config('DEBUG', default=True, cast=bool)
ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost').split(',')
ENVIRONMENT = config('ENVIRONMENT', default='development')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'django_filters',
    'storages',
    'django_ratelimit',
    'apps.accounts',
    'apps.streaming',
    'apps.payments',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# ============ DATABASE ============
if ENVIRONMENT == 'production':
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': config('DB_NAME', default='youtubestreamer'),
            'USER': config('DB_USER', default='postgres'),
            'PASSWORD': config('DB_PASSWORD', default='postgres'),
            'HOST': config('DB_HOST', default='localhost'),
            'PORT': config('DB_PORT', default='5432'),
            'OPTIONS': {'connect_timeout': 10},
            # Connection pooling — important for long-running Celery workers
            'CONN_MAX_AGE': 60,
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ============ AWS S3 STORAGE ============
if ENVIRONMENT == 'production':
    AWS_ACCESS_KEY_ID = config('AWS_ACCESS_KEY_ID')
    AWS_SECRET_ACCESS_KEY = config('AWS_SECRET_ACCESS_KEY')
    AWS_STORAGE_BUCKET_NAME = config('AWS_STORAGE_BUCKET_NAME')
    AWS_S3_REGION_NAME = config('AWS_S3_REGION_NAME', default='us-east-1')
    AWS_S3_CUSTOM_DOMAIN = f'{AWS_STORAGE_BUCKET_NAME}.s3.{AWS_S3_REGION_NAME}.amazonaws.com'
    AWS_LOCATION = 'media'
    AWS_DEFAULT_ACL = None
    AWS_S3_FILE_OVERWRITE = False
    AWS_S3_OBJECT_PARAMETERS = {'CacheControl': 'max-age=86400'}

    STATIC_URL = f'https://{AWS_S3_CUSTOM_DOMAIN}/static/'
    STATIC_ROOT = 'static'
    STATICFILES_STORAGE = 'storages.backends.s3boto3.S3Boto3Storage'

    MEDIA_URL = f'https://{AWS_S3_CUSTOM_DOMAIN}/media/'
    MEDIA_ROOT = 'media'
    DEFAULT_FILE_STORAGE = 'storages.backends.s3boto3.S3Boto3Storage'

    STORAGES = {
        'default': {
            'BACKEND': 'storages.backends.s3boto3.S3Boto3Storage',
            'OPTIONS': {
                'bucket_name': AWS_STORAGE_BUCKET_NAME,
                'region_name': AWS_S3_REGION_NAME,
            },
        },
        'staticfiles': {
            'BACKEND': 'storages.backends.s3boto3.S3Boto3Storage',
            'OPTIONS': {
                'bucket_name': AWS_STORAGE_BUCKET_NAME,
                'region_name': AWS_S3_REGION_NAME,
            },
        },
    }
else:
    STATIC_URL = '/static/'
    STATICFILES_DIRS = [BASE_DIR / 'static']
    STATIC_ROOT = BASE_DIR / 'staticfiles'

    MEDIA_URL = '/media/'
    MEDIA_ROOT = BASE_DIR / 'media'

# ============ CACHE ============
# Use Redis for cache in both dev and production (required for django_ratelimit)
CACHES = {
    'default': {
        'BACKEND': 'django_redis.cache.RedisCache',
        'LOCATION': config('REDIS_URL', default='redis://localhost:6379/1'),
        'OPTIONS': {
            'CLIENT_CLASS': 'django_redis.client.DefaultClient',
            'IGNORE_EXCEPTIONS': True,
            # Keep stream heartbeat keys alive across reconnects
            'SOCKET_CONNECT_TIMEOUT': 5,
            'SOCKET_TIMEOUT': 5,
        },
    }
}

# ============ SESSION ============
# Use database for sessions in development (don't require Redis running)
# Production can use cache sessions if needed
if ENVIRONMENT == 'production':
    SESSION_ENGINE = 'django.contrib.sessions.backends.cache'
    SESSION_CACHE_ALIAS = 'default'
else:
    SESSION_ENGINE = 'django.contrib.sessions.backends.db'

# ============ GOOGLE OAUTH ============
GOOGLE_CLIENT_ID = config('GOOGLE_CLIENT_ID')
GOOGLE_CLIENT_SECRET = config('GOOGLE_CLIENT_SECRET')
GOOGLE_REDIRECT_URI = config('GOOGLE_REDIRECT_URI')
GOOGLE_SCOPES = [
    'https://www.googleapis.com/auth/youtube',
    'https://www.googleapis.com/auth/youtube.force-ssl',
]

# ============ RAZORPAY ============
RAZORPAY_KEY_ID = config('RAZORPAY_KEY_ID')
RAZORPAY_KEY_SECRET = config('RAZORPAY_KEY_SECRET')

# ✅ Keep only this one at the bottom
FFMPEG_PATH = config('FFMPEG_PATH', default='/usr/bin/ffmpeg')

# ============ SUBSCRIPTION PLANS ============
SUBSCRIPTION_PLANS = {
    'oneday': {
        'name': 'One Day Plan',
        'price': 4900.00,
        'duration_days': 1,
        'max_streams': 1,
        'storage_limit': 5 * 1024 ** 3,   # 5 GB
        'description': '1 concurrent stream, 1 day access',
    },
    'monthly': {
        'name': 'Monthly Plan',
        'price': 49900.00,
        'duration_days': 30,
        'max_streams': 1,
        'storage_limit': 20 * 1024 ** 3,  # 20 GB
        'description': '1 concurrent stream, 30 days access',
    },
    'annual': {
        'name': 'Annual Plan',
        'price': 399900.00,
        'duration_days': 365,
        'max_streams': 3,
        'storage_limit': 100 * 1024 ** 3, # 100 GB
        'description': 'Up to 3 concurrent streams, 365 days access',
    },
}

# ============ CELERY ============
CELERY_BROKER_URL = config('CELERY_BROKER_URL', default='redis://localhost:6379/0')
CELERY_RESULT_BACKEND = config('CELERY_RESULT_BACKEND', default='redis://localhost:6379/0')
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE

# Long-running stream tasks need a high visibility timeout so the broker
# doesn't re-queue them while FFmpeg is still running (default is 1 h).
# Set to 25 hours — slightly above the 24 h task time_limit.
CELERY_BROKER_TRANSPORT_OPTIONS = {
    'visibility_timeout': 25 * 3600,  # 25 hours
}

# Workers should only pick up one task at a time so a single worker
# running a 24-hour stream task doesn't starve other tasks.
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True

# Results expire after 6 hours (stream tasks store minimal results)
CELERY_RESULT_EXPIRES = 6 * 3600

# Task routing — streaming tasks get their own queue so health checks
# and cleanup tasks don't compete with long-running stream workers.
CELERY_TASK_ROUTES = {
    # Long-running stream tasks → dedicated 'streaming' queue
    'apps.streaming.tasks.stream_playlist_direct_async': {'queue': 'streaming'},
    'apps.streaming.tasks.start_stream_async': {'queue': 'streaming'},
    'apps.streaming.tasks.stop_stream_async': {'queue': 'streaming'},
    'apps.streaming.tasks.restart_stream_async': {'queue': 'streaming'},
    'apps.streaming.tasks.download_playlist_videos_async': {'queue': 'streaming'},
    # Short periodic tasks → default queue
    'apps.streaming.tasks.start_scheduled_streams': {'queue': 'celery'},
    'apps.streaming.tasks.check_stream_health': {'queue': 'celery'},
    'apps.streaming.tasks.cleanup_old_logs': {'queue': 'celery'},
    'apps.payments.tasks.*': {'queue': 'celery'},
}

# ============ FFMPEG / STREAMING ============
FFMPEG_PATH = config('FFMPEG_PATH', default='ffmpeg')

# Scratch directory for FIFOs and temp concat files.
# Must be on a local filesystem (not NFS/S3-mount) — FIFOs require local FS.
STREAM_TEMP_DIR = config('STREAM_TEMP_DIR', default='/var/tmp/streams')

# Max parallel S3 downloads when streaming from stored MediaFiles
MAX_CONCURRENT_DOWNLOADS = config('MAX_CONCURRENT_DOWNLOADS', default=3, cast=int)

# Max videos fetched from a YouTube playlist per stream start
PLAYLIST_MAX_VIDEOS = config('PLAYLIST_MAX_VIDEOS', default=50, cast=int)

# ============ REST FRAMEWORK ============
REST_FRAMEWORK = {
    'DEFAULT_FILTER_BACKENDS': ['django_filters.rest_framework.DjangoFilterBackend'],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
}

# ============ AUTH ============
LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/dashboard/'
LOGOUT_REDIRECT_URL = '/'

# ============ LOGGING ============
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '[{asctime}] {levelname} {name} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'loggers': {
        'apps.streaming': {
            'handlers': ['console'],
            'level': 'DEBUG' if DEBUG else 'INFO',
            'propagate': False,
        },
        'celery': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'WARNING',
    },
}
