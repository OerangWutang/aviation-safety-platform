from datetime import timedelta

from celery import shared_task
from django.db import models, transaction
from django.utils import timezone

from reports.models import Report

from .models import IngestionPayload, OutboxEvent
from .storage import fetch_json_payload


@shared_task
def parse_feed_task(payload_id):
    payload = IngestionPayload.objects.get(pk=payload_id)
    payload.processing_status = IngestionPayload.ProcessingStatus.PROCESSING
    payload.save(update_fields=["processing_status", "updated_at"])

    data = fetch_json_payload(payload.s3_blob_key)
    reports_data = data if isinstance(data, list) else [data]

    with transaction.atomic():
        for row in reports_data:
            external_id = row.get("external_source_id") or row.get("id") or str(payload.id)
            tracking_number = row.get("tracking_number") or f"ING-{external_id}"
            report, created = Report.objects.update_or_create(
                organization=payload.organization,
                tracking_number=tracking_number,
                defaults={
                    "created_by": payload.organization.users.order_by("created_at").first(),
                    "ingestion_payload": payload,
                    "external_source_id": str(external_id),
                    "narrative": row.get("narrative", ""),
                    "status": Report.Status.INGESTED,
                    "org_source_external_id": f"{payload.organization_id}:{payload.source}:{external_id}",
                },
            )
            OutboxEvent.objects.create(
                organization=payload.organization,
                aggregate_id=report.id,
                aggregate_type="report",
                event_type="report.ingested" if created else "report.updated",
                payload={"report_id": str(report.id)},
                available_at=timezone.now(),
            )

        payload.processing_status = IngestionPayload.ProcessingStatus.PROCESSED
        payload.processed_at = timezone.now()
        payload.save(update_fields=["processing_status", "processed_at", "updated_at"])


@shared_task
def requeue_stuck_outbox_events():
    cutoff = timezone.now() - timedelta(minutes=5)
    return OutboxEvent.objects.filter(
        publish_status=OutboxEvent.PublishStatus.PENDING,
        available_at__lt=cutoff,
    ).update(available_at=timezone.now(), attempts=models.F("attempts") + 1)
