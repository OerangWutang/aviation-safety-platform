from django.db import transaction
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import IsTenantMember
from .models import Report
from .serializers import (
    ReportCreateSerializer,
    ReportDetailSerializer,
    ReportListSerializer,
    ReportStatusTransitionSerializer,
)
from .tasks import refresh_report_read_model_task


class ReportListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsTenantMember]

    def get_serializer_class(self):
        if self.request.method == "POST":
            return ReportCreateSerializer
        return ReportListSerializer

    def get_queryset(self):
        qs = (
            Report.objects
            .filter(organization=self.request.user.organization)
            .select_related("created_by")
        )
        status_filter = self.request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        return qs

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        report = serializer.save()
        transaction.on_commit(
            lambda: refresh_report_read_model_task.delay(str(report.id))
        )
        return Response(
            ReportDetailSerializer(report).data,
            status=status.HTTP_201_CREATED,
        )


class ReportDetailView(generics.RetrieveUpdateAPIView):
    permission_classes = [IsTenantMember]
    serializer_class = ReportDetailSerializer
    lookup_field = "id"

    def get_queryset(self):
        return Report.objects.filter(
            organization=self.request.user.organization
        ).select_related("organization", "created_by")

    def get_object(self):
        obj = super().get_object()
        self.check_object_permissions(self.request, obj)
        return obj


class ReportTransitionView(APIView):
    permission_classes = [IsTenantMember]

    def post(self, request, id):
        with transaction.atomic():
            try:
                report = Report.objects.select_for_update().get(
                    id=id, organization=request.user.organization
                )
            except Report.DoesNotExist:
                return Response(
                    {"detail": "Not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            self.check_object_permissions(request, report)
            serializer = ReportStatusTransitionSerializer(
                data=request.data, context={"report": report},
            )
            serializer.is_valid(raise_exception=True)
            report.transition_to(serializer.validated_data["status"])
            report.save(update_fields=["status", "version", "updated_at"])
        transaction.on_commit(
            lambda: refresh_report_read_model_task.delay(str(report.id))
        )
        return Response(ReportDetailSerializer(report).data)
