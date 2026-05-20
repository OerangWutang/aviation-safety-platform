import os
from celery import Celery

if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    raise RuntimeError(
        "DJANGO_SETTINGS_MODULE is not set. "
        "Export it before starting the Celery worker, "
        "e.g. export DJANGO_SETTINGS_MODULE=config.settings.production"
    )

app = Celery("aviation_safety")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
