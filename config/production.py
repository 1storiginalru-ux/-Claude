"""Server settings. Local Mac startup continues to use config.settings."""
from .settings import *  # noqa: F403
from django.core.exceptions import ImproperlyConfigured


DEBUG = False
if not os.environ.get('DJANGO_SECRET_KEY'):
    raise ImproperlyConfigured('Для сервера задайте DJANGO_SECRET_KEY в /etc/voice/voice.env.')

ALLOWED_HOSTS = [host.strip() for host in os.environ.get('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',') if host.strip()]
if not ALLOWED_HOSTS or '*' in ALLOWED_HOSTS:
    raise ImproperlyConfigured('Укажите конкретные DJANGO_ALLOWED_HOSTS для сервера.')

DATABASES = {'default': {
    'ENGINE': 'django.db.backends.postgresql',
    'NAME': os.environ.get('PGDATABASE', 'voice'),
    'USER': os.environ.get('PGUSER', 'voice'),
    'PASSWORD': os.environ.get('PGPASSWORD', ''),
    'HOST': os.environ.get('PGHOST', '/var/run/postgresql'),
    'PORT': os.environ.get('PGPORT', '5432'),
    'CONN_MAX_AGE': 60,
    'CONN_HEALTH_CHECKS': True,
    'OPTIONS': {'connect_timeout': 5, 'application_name': 'voice'},
}}

# Gunicorn listens only on loopback; the HTTPS proxy replaces this header.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = SESSION_COOKIE_SECURE
CSRF_TRUSTED_ORIGINS = [origin.strip() for origin in os.environ.get('DJANGO_CSRF_TRUSTED_ORIGINS', '').split(',') if origin.strip()]
SECURE_HSTS_SECONDS = 86400 if SESSION_COOKIE_SECURE else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
STATIC_ROOT = STATE_DIR / 'static'
MIDDLEWARE = [*MIDDLEWARE[:1], 'whitenoise.middleware.WhiteNoiseMiddleware', *MIDDLEWARE[1:]]
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'},
}
