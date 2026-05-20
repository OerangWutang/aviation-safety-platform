import os
from django.core.asgi import get_asgi_application
from django.core.exceptions import ImproperlyConfigured

if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    raise ImproperlyConfigured(
        "DJANGO_SETTINGS_MODULE environment variable must be set. "
        "Set it to 'config.settings.local' for development or 'config.settings.production' for production."
    )

application = get_asgi_application()
