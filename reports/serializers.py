from django.utils import timezone
from rest_framework import serializers

from .models import OrgReportsReadModel, Report, ReportReadModel, ReportReview


class ReportSerializer(serializers.ModelSerializer):
    class Meta:
        model = Report
        fields = [
            "id",
            "organization",
            "created_by",
            "ingestion_payload",
            "event_type",
            "location_node",
            "tracking_number",
            "external_source_id",
            "source_url",
            "event_date",
            "narrative",
            "status",
            "version",
            "org_source_external_id",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["organization", "created_by", "version", "created_at", "updated_at"]

    def validate(self, attrs):
        instance = getattr(self, "instance", None)
        if not instance:
            return attrs
        current = instance.status
        nxt = attrs.get("status", current)
        allowed = {
            Report.Status.DRAFT: {Report.Status.UNDER_REVIEW, Report.Status.ARCHIVED},
            Report.Status.UNDER_REVIEW: {Report.Status.REQUIRES_REVISION, Report.Status.APPROVED_QUEUED, Report.Status.REJECTED},
            Report.Status.REQUIRES_REVISION: {Report.Status.UNDER_REVIEW, Report.Status.ARCHIVED},
            Report.Status.APPROVED_QUEUED: {Report.Status.PUBLISHED, Report.Status.REJECTED},
        }
        if current != nxt and nxt not in allowed.get(current, set()):
            raise serializers.ValidationError({"status": f"Invalid transition {current} -> {nxt}"})
        return attrs

    def create(self, validated_data):
        user = self.context["request"].user
        validated_data["organization"] = user.organization
        validated_data["created_by"] = user
        if "org_source_external_id" not in validated_data:
            ext = validated_data.get("external_source_id", "manual")
            validated_data["org_source_external_id"] = f"{user.organization_id}:manual:{ext}"
        return super().create(validated_data)

    def update(self, instance, validated_data):
        status_changed = "status" in validated_data and validated_data["status"] != instance.status
        updated = super().update(instance, validated_data)
        if status_changed:
            updated.version += 1
            updated.save(update_fields=["version", "updated_at"])
        return updated


class ReportReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReportReview
        fields = ["id", "report", "reviewer", "report_version", "review_stage", "decision", "comment", "decided_at", "created_at"]
        read_only_fields = ["report", "reviewer", "report_version", "created_at"]

    def create(self, validated_data):
        report = self.context["report"]
        user = self.context["request"].user
        validated_data["report"] = report
        validated_data["reviewer"] = user
        validated_data["report_version"] = report.version
        validated_data.setdefault("decided_at", timezone.now())
        return super().create(validated_data)


class ReportReadModelSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReportReadModel
        fields = ["report", "document", "document_version", "refreshed_at"]


class OrgReportsReadModelSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrgReportsReadModel
        fields = ["id", "organization", "view_key", "document", "document_version", "refreshed_at"]
