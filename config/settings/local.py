from .base import *  # noqa
import environ

env = environ.Env()

DEBUG = True

ALLOWED_HOSTS = ['*']

CORS_ALLOW_ALL_ORIGINS = True
