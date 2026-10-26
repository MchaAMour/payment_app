"""
Django settings for the payment demo.

Everything environment-specific comes from environment variables (optionally loaded from a
.env file). Local development works with no configuration beyond the Stripe keys; running with
DJANGO_DEBUG=0 (production) requires DJANGO_SECRET_KEY and DJANGO_ALLOWED_HOSTS.
"""
import os
from datetime import timedelta
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')
load_dotenv(BASE_DIR.parent.parent / '.env')  # fallback: payment/.env, kept outside the repo


def env_bool(name, default):
    return os.environ.get(name, default).strip().lower() in ('1', 'true', 'yes', 'on')


def env_list(name, default=''):
    return [v.strip() for v in os.environ.get(name, default).split(',') if v.strip()]


# --- core -------------------------------------------------------------------------------------

DEBUG = env_bool('DJANGO_DEBUG', '1')

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', '')
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured('DJANGO_SECRET_KEY is required when DJANGO_DEBUG is off')
    SECRET_KEY = 'insecure-dev-key-do-not-use-in-production'

ALLOWED_HOSTS = env_list('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1,[::1]')
if not DEBUG and not os.environ.get('DJANGO_ALLOWED_HOSTS'):
    raise ImproperlyConfigured('DJANGO_ALLOWED_HOSTS is required when DJANGO_DEBUG is off')
CSRF_TRUSTED_ORIGINS = env_list('DJANGO_CSRF_TRUSTED_ORIGINS')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'payments',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',  # serves admin static files behind gunicorn
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'
WSGI_APPLICATION = 'config.wsgi.application'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

# --- database (PostgreSQL: the design needs ACID transactions and row locks) --------------------

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('DB_NAME', 'payments'),
        'USER': os.environ.get('DB_USER', 'payments'),
        'PASSWORD': os.environ.get('DB_PASSWORD', 'payments'),
        'HOST': os.environ.get('DB_HOST', 'localhost'),
        'PORT': os.environ.get('DB_PORT', '5433'),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedStaticFilesStorage'},
}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# --- Stripe ------------------------------------------------------------------------------------

STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY', '')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
# Optional: point the Stripe client elsewhere (e.g. http://127.0.0.1:9) to simulate a PSP outage.
STRIPE_API_BASE = os.environ.get('STRIPE_API_BASE', '')

# --- API auth (JWT) ----------------------------------------------------------------------------

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': ['rest_framework_simplejwt.authentication.JWTAuthentication'],
    'DEFAULT_PERMISSION_CLASSES': ['rest_framework.permissions.IsAuthenticated'],
}
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=30),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=1),
}
# Self-service sign-up. Turn off on a public server and create accounts with `manage.py seed_demo`.
ALLOW_REGISTRATION = env_bool('ALLOW_REGISTRATION', '1')

# --- Celery (retry queue for PSP calls). Redis runs from docker-compose on :6380. ---------------

CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', 'redis://localhost:6380/0')
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_TASK_ACKS_LATE = True               # a task is only acked after it finishes...
CELERY_TASK_REJECT_ON_WORKER_LOST = True   # ...so a killed worker's task is redelivered
PAYMENT_MAX_RETRIES = int(os.environ.get('PAYMENT_MAX_RETRIES', '5'))  # then the dead letter queue
PAYMENT_RETRY_BASE_SECONDS = int(os.environ.get('PAYMENT_RETRY_BASE_SECONDS', '2'))

# --- production hardening (only when DJANGO_DEBUG is off) ---------------------------------------

if env_bool('DJANGO_BEHIND_PROXY', '0'):  # TLS is terminated by Caddy/nginx in front of gunicorn
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = env_bool('DJANGO_BEHIND_PROXY', '0')
    SECURE_HSTS_SECONDS = 3600
    SECURE_CONTENT_TYPE_NOSNIFF = True

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'console': {'class': 'logging.StreamHandler'}},
    'root': {'handlers': ['console'], 'level': 'WARNING'},
    'loggers': {'payments': {'handlers': ['console'], 'level': 'INFO', 'propagate': False}},
}
