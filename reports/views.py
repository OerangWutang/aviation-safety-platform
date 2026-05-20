from django.core.cache import cache
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from core.cache_keys import reports_cache_key
from core.permissions import IsTenantScoped

from .models import OrgReportsReadModel, Report, ReportReadModel
from .serializers import ReportReviewSerializer, ReportSerializer
from .tasks import refresh_org_read_model_task, refresh_read_model_task


class ReportViewSet(viewsets.ModelViewSet):
    serializer_class = ReportSerializer
    permission_classes = [IsTenantScoped]

    def get_queryset(self):
        return Report.objects.filter(organization_id=self.request.organization_id).order_by("-created_at")

    def list(self, request, *args, **kwargs):
        org_id = request.organization_id
        status_filter = request.query_params.get("status", "")
        location_filter = request.query_params.get("location", "")
        filters = f"status={status_filter}|location={location_filter}"
        key = reports_cache_key(org_id, "list", filters)
        cached = cache.get(key)
        if cached is not None:
            return Response(cached)

        view_key = f"org:{org_id}:reports"
        orm = OrgReportsReadModel.objects.filter(organization_id=org_id, view_key=view_key).first()
        rows = orm.document.get("reports", []) if orm else list(
            self.get_queryset().values("id", "tracking_number", "status", "event_date", "updated_at")
        )
        if status_filter:
            rows = [r for r in rows if r.get("status") == status_filter]
        if location_filter:
            rows = [r for r in rows if str(r.get("location_node", "")) == location_filter]

        cache.set(key, rows, timeout=60)
        return Response(rows)

    def retrieve(self, request, *args, **kwargs):
        report = self.get_object()
        rm = ReportReadModel.objects.filter(report=report).first()
        if rm:
            return Response(rm.document)
        serializer = self.get_serializer(report)
        return Response(serializer.data)

    def perform_create(self, serializer):
        report = serializer.save()
        refresh_read_model_task.delay(str(report.id))
        refresh_org_read_model_task.delay(str(report.organization_id))

    def perform_update(self, serializer):
        report = serializer.save()
        refresh_read_model_task.delay(str(report.id))
        refresh_org_read_model_task.delay(str(report.organization_id))

    @action(detail=True, methods=["post"], url_path="reviews")
    def reviews(self, request, pk=None):
        report = get_object_or_404(self.get_queryset(), pk=pk)
        serializer = ReportReviewSerializer(data=request.data, context={"request": request, "report": report})
        serializer.is_valid(raise_exception=True)
        review = serializer.save()
        return Response(ReportReviewSerializer(review).data, status=status.HTTP_201_CREATED)
