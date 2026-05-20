from django.db import models

from core.models import TimestampedModel


class IngestionPayload(TimestampedModel):
    class ProcessingStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        QUEUED = "queued", "Queued"
        PROCESSING = "processing", "Processing"
        PROCESSED = "processed", "Processed"
        FAILED = "failed", "Failed"

    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="ingestion_payloads")
    source = models.CharField(max_length=100)
    external_source_id = models.CharField(max_length=255)
    s3_blob_key = models.CharField(max_length=512)
    processing_status = models.CharField(max_length=16, choices=ProcessingStatus.choices, default=ProcessingStatus.PENDING)
    idempotency_key = models.CharField(max_length=255, unique=True)
    payload_hash = models.CharField(max_length=128)
    received_at = models.DateTimeField(auto_now_add=True)
    queued_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "source", "external_source_id"], name="uniq_org_source_external"),
        ]

    def __str__(self):
        return f"{self.source}:{self.external_source_id}"


class IngestionEvent(TimestampedModel):
    class EventType(models.TextChoices):
        INFO = "info", "Info"
        WARNING = "warning", "Warning"
        ERROR = "error", "Error"
        DEBUG = "debug", "Debug"

    ingestion_payload = models.ForeignKey(IngestionPayload, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=16, choices=EventType.choices)
    message = models.TextField()
    metadata = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"{self.event_type}:{self.message[:40]}"


class OutboxEvent(TimestampedModel):
    class PublishStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        PUBLISHED = "published", "Published"
        FAILED = "failed", "Failed"

    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="outbox_events")
    aggregate_id = models.UUIDField()
    aggregate_type = models.CharField(max_length=64)
    event_type = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)
    publish_status = models.CharField(max_length=16, choices=PublishStatus.choices, default=PublishStatus.PENDING)
    attempts = models.IntegerField(default=0)
    available_at = models.DateTimeField()
    published_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.aggregate_type}:{self.event_type}"
