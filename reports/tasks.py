import logging
from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def refresh_report_read_model_task(self, report_id: str):
    from .models import Report, ReportReadModel
    try:
        report = (
            Report.objects
            .select_related("organization", "created_by")
            .get(id=report_id)
        )
    except Report.DoesNotExist:
        logger.warning("refresh_report_read_model_task: Report %s not found", report_id)
        return

    document = {
        "id": str(report.id),
        "tracking_number": report.tracking_number,
        "status": report.status,
        "event_date": report.event_date.isoformat() if report.event_date else None,
        "narrative": report.narrative,
        "organization_id": str(report.organization_id),
        "organization_name": report.organization.name,
        "created_by_id": str(report.created_by_id),
        "created_by_email": report.created_by.email,
        "version": report.version,
        "created_at": report.created_at.isoformat(),
        "updated_at": report.updated_at.isoformat(),
    }
    ReportReadModel.objects.update_or_create(
        report=report,
        defaults={
            "document": document,
            "document_version": report.version,
            "refreshed_at": timezone.now(),
        },
    )
    logger.info("Refreshed read model for Report %s", report_id)
