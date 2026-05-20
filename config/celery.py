import os
from celery import Celery

if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    raise RuntimeError(
        "DJANGO_SETTINGS_MODULE environment variable must be set. "
        "Set it to 'config.settings.local' for development or 'config.settings.production' for production."
    )

app = Celery("aviation_safety")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
