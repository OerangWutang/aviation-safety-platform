from django.urls import path
from .views import ReportListCreateView, ReportDetailView, ReportTransitionView

urlpatterns = [
    path("reports/", ReportListCreateView.as_view(), name="report_list_create"),
    path("reports/<uuid:id>/", ReportDetailView.as_view(), name="report_detail"),
    path("reports/<uuid:id>/transition/", ReportTransitionView.as_view(), name="report_transition"),
]
