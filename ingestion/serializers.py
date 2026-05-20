import hashlib
import json
import uuid

from django.utils import timezone
from rest_framework import serializers

from .models import IngestionPayload, OutboxEvent


class IngestPayloadSerializer(serializers.Serializer):
    source = serializers.CharField(max_length=100)
    external_source_id = serializers.CharField(max_length=255)
    source_updated_at = serializers.DateTimeField(required=False)
    payload = serializers.JSONField()

    def create(self, validated_data):
        request = self.context["request"]
        org = request.user.organization
        payload = validated_data["payload"]
        payload_json = json.dumps(payload, sort_keys=True)
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        idempotency_key = self.context.get("idempotency_key") or str(uuid.uuid4())

        ingestion_payload = IngestionPayload.objects.create(
            organization=org,
            source=validated_data["source"],
            external_source_id=validated_data["external_source_id"],
            s3_blob_key=self.context["s3_blob_key"],
            processing_status=IngestionPayload.ProcessingStatus.QUEUED,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            queued_at=timezone.now(),
            source_updated_at=validated_data.get("source_updated_at"),
        )

        OutboxEvent.objects.create(
            organization=org,
            aggregate_id=ingestion_payload.id,
            aggregate_type="ingestion_payload",
            event_type="ingestion_payload.received",
            payload={"ingestion_payload_id": str(ingestion_payload.id)},
            available_at=timezone.now(),
        )
        return ingestion_payload
