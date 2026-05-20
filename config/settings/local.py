import environ
from .base import *  # noqa: F401,F403
from .base import BASE_DIR

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

DEBUG = True
ALLOWED_HOSTS = ["*"]
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
