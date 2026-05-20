from django.db import models

from core.models import TimestampedModel


class Report(TimestampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        INGESTED = "ingested", "Ingested"
        VALIDATION_FAILED = "validation_failed", "Validation Failed"
        UNDER_REVIEW = "under_review", "Under Review"
        REQUIRES_REVISION = "requires_revision", "Requires Revision"
        APPROVED_QUEUED = "approved_queued", "Approved Queued"
        PUBLISHED = "published", "Published"
        REJECTED = "rejected", "Rejected"
        ARCHIVED = "archived", "Archived"

    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="reports")
    created_by = models.ForeignKey("users.AppUser", on_delete=models.CASCADE, related_name="reports")
    ingestion_payload = models.ForeignKey(
        "ingestion.IngestionPayload", on_delete=models.SET_NULL, null=True, blank=True, related_name="reports"
    )
    event_type = models.ForeignKey(
        "taxonomy.TaxonomyNode", on_delete=models.SET_NULL, null=True, blank=True, related_name="event_reports"
    )
    location_node = models.ForeignKey(
        "taxonomy.LocationNode", on_delete=models.SET_NULL, null=True, blank=True, related_name="location_reports"
    )
    tracking_number = models.CharField(max_length=100, unique=True)
    external_source_id = models.CharField(max_length=255, blank=True)
    source_url = models.URLField(blank=True)
    event_date = models.DateField(null=True, blank=True)
    narrative = models.TextField(blank=True)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.DRAFT)
    version = models.IntegerField(default=1)
    org_source_external_id = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "source_url", "external_source_id"], name="uniq_org_src_external"),
        ]

    def __str__(self):
        return self.tracking_number


class ReportReview(TimestampedModel):
    class ReviewStage(models.TextChoices):
        TRIAGE = "triage", "Triage"
        FIRST_REVIEW = "first_review", "First Review"
        FINAL_REVIEW = "final_review", "Final Review"

    class Decision(models.TextChoices):
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        REVISION_REQUESTED = "revision_requested", "Revision Requested"

    report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="reviews")
    reviewer = models.ForeignKey("users.AppUser", on_delete=models.CASCADE, related_name="report_reviews")
    report_version = models.IntegerField()
    review_stage = models.CharField(max_length=20, choices=ReviewStage.choices)
    decision = models.CharField(max_length=20, choices=Decision.choices)
    comment = models.TextField(blank=True)
    decided_at = models.DateTimeField()

    def __str__(self):
        return f"{self.report.tracking_number}:{self.decision}"


class ReportAttachment(TimestampedModel):
    class ScanStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        CLEAN = "clean", "Clean"
        INFECTED = "infected", "Infected"

    class RedactionStatus(models.TextChoices):
        NONE = "none", "None"
        PENDING = "pending", "Pending"
        REDACTED = "redacted", "Redacted"

    report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="attachments")
    uploaded_by = models.ForeignKey("users.AppUser", on_delete=models.CASCADE, related_name="uploaded_attachments")
    s3_blob_key = models.CharField(max_length=512)
    file_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    file_size_bytes = models.BigIntegerField()
    checksum = models.CharField(max_length=64, unique=True)
    scan_status = models.CharField(max_length=16, choices=ScanStatus.choices, default=ScanStatus.PENDING)
    redaction_status = models.CharField(max_length=16, choices=RedactionStatus.choices, default=RedactionStatus.NONE)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.file_name


class ReportReadModel(models.Model):
    report = models.OneToOneField(Report, on_delete=models.CASCADE, primary_key=True, related_name="read_model")
    document = models.JSONField(default=dict)
    document_version = models.IntegerField(default=1)
    refreshed_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"ReadModel:{self.report_id}"


class OrgReportsReadModel(TimestampedModel):
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="org_read_models")
    view_key = models.CharField(max_length=100, unique=True)
    document = models.JSONField(default=dict)
    document_version = models.IntegerField(default=1)
    refreshed_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.view_key
