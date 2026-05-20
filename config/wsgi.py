import os
from django.core.wsgi import get_wsgi_application

if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    raise RuntimeError(
        "DJANGO_SETTINGS_MODULE is not set. "
        "Export it before starting the WSGI server, "
        "e.g. export DJANGO_SETTINGS_MODULE=config.settings.production"
    )

application = get_wsgi_application()
