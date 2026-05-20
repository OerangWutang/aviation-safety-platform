import uuid
import secrets
from django.conf import settings
from django.db import models
from django.utils import timezone
from core.models import TimestampedModel


class ReportStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    INGESTED = "ingested", "Ingested"
    VALIDATION_FAILED = "validation_failed", "Validation Failed"
    UNDER_REVIEW = "under_review", "Under Review"
    REQUIRES_REVISION = "requires_revision", "Requires Revision"
    APPROVED_QUEUED = "approved_queued", "Approved – Post-processing Queued"
    PUBLISHED = "published", "Published"
    REJECTED = "rejected", "Rejected"
    ARCHIVED = "archived", "Archived"


STATUS_TRANSITIONS: dict[str, list[str]] = {
    ReportStatus.DRAFT: [ReportStatus.UNDER_REVIEW],
    ReportStatus.INGESTED: [ReportStatus.UNDER_REVIEW],
    ReportStatus.VALIDATION_FAILED: [ReportStatus.ARCHIVED],
    ReportStatus.UNDER_REVIEW: [
        ReportStatus.REQUIRES_REVISION,
        ReportStatus.APPROVED_QUEUED,
        ReportStatus.REJECTED,
    ],
    ReportStatus.REQUIRES_REVISION: [ReportStatus.UNDER_REVIEW],
    ReportStatus.APPROVED_QUEUED: [ReportStatus.PUBLISHED],
    ReportStatus.PUBLISHED: [ReportStatus.ARCHIVED],
    ReportStatus.REJECTED: [ReportStatus.ARCHIVED],
    ReportStatus.ARCHIVED: [],
}


def _generate_tracking_number() -> str:
    return "ASP-" + secrets.token_hex(4).upper()


class Report(TimestampedModel):
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.PROTECT, related_name="reports",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_reports",
    )
    tracking_number = models.CharField(
        max_length=32, unique=True, default=_generate_tracking_number, editable=False,
    )
    event_date = models.DateField(null=True, blank=True)
    narrative = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=30, choices=ReportStatus.choices, default=ReportStatus.DRAFT, db_index=True,
    )
    version = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["organization", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.tracking_number} [{self.status}]"

    def can_transition_to(self, new_status: str) -> bool:
        return new_status in STATUS_TRANSITIONS.get(self.status, [])

    def transition_to(self, new_status: str) -> None:
        if not self.can_transition_to(new_status):
            raise ValueError(f"Cannot transition from '{self.status}' to '{new_status}'.")
        self.status = new_status
        self.version += 1


class ReportReadModel(models.Model):
    report = models.OneToOneField(
        Report, on_delete=models.CASCADE, primary_key=True, related_name="read_model",
    )
    document = models.JSONField(default=dict)
    document_version = models.PositiveIntegerField(default=1)
    refreshed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Report read model"

    def __str__(self):
        return f"ReadModel({self.report_id})"
