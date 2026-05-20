from django.contrib import admin

from .models import IngestionEvent, IngestionPayload, OutboxEvent

admin.site.register(IngestionPayload)
admin.site.register(IngestionEvent)
admin.site.register(OutboxEvent)
