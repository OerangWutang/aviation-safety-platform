from rest_framework import serializers
from .models import Report, ReportStatus, STATUS_TRANSITIONS


class ReportCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Report
        fields = ["event_date", "narrative"]

    def create(self, validated_data):
        request = self.context["request"]
        validated_data["organization"] = request.user.organization
        validated_data["created_by"] = request.user
        return super().create(validated_data)


class ReportListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Report
        fields = ["id", "tracking_number", "status", "event_date", "created_by", "created_at", "version"]


class ReportDetailSerializer(serializers.ModelSerializer):
    allowed_transitions = serializers.SerializerMethodField()

    class Meta:
        model = Report
        fields = [
            "id", "tracking_number", "organization", "created_by",
            "event_date", "narrative", "status", "version",
            "created_at", "updated_at", "allowed_transitions",
        ]
        read_only_fields = [
            "id", "tracking_number", "organization", "created_by",
            "created_at", "updated_at", "version",
        ]

    def get_allowed_transitions(self, obj) -> list[str]:
        return STATUS_TRANSITIONS.get(obj.status, [])


class ReportStatusTransitionSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=ReportStatus.choices)

    def validate_status(self, new_status):
        report = self.context["report"]
        if not report.can_transition_to(new_status):
            raise serializers.ValidationError(
                f"Cannot transition from '{report.status}' to '{new_status}'."
            )
        return new_status
