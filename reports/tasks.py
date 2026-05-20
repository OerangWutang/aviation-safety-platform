from celery import shared_task
from django.core.cache import cache
from django.forms.models import model_to_dict

from core.cache_keys import reports_cache_key

from .models import OrgReportsReadModel, Report, ReportReadModel


@shared_task
def refresh_read_model_task(report_id):
    report = Report.objects.select_related("organization", "created_by").get(pk=report_id)
    document = model_to_dict(
        report,
        fields=[
            "id",
            "tracking_number",
            "external_source_id",
            "source_url",
            "narrative",
            "status",
            "version",
            "org_source_external_id",
        ],
    )
    ReportReadModel.objects.update_or_create(
        report=report,
        defaults={"document": document, "document_version": report.version},
    )


@shared_task
def refresh_org_read_model_task(organization_id):
    raw_reports = Report.objects.filter(organization_id=organization_id).values(
        "id", "tracking_number", "status", "event_date", "updated_at"
    )
    reports = [
        {
            "id": str(row["id"]),
            "tracking_number": row["tracking_number"],
            "status": row["status"],
            "event_date": row["event_date"].isoformat() if row["event_date"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }
        for row in raw_reports
    ]
    document = {"reports": reports}
    OrgReportsReadModel.objects.update_or_create(
        organization_id=organization_id,
        view_key=f"org:{organization_id}:reports",
        defaults={"document": document, "document_version": 1},
    )
    if hasattr(cache, "delete_pattern"):
        cache.delete_pattern(f"reports:{organization_id}:*")
    cache.delete(reports_cache_key(organization_id, "list", "all"))
