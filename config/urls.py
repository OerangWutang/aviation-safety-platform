from django.contrib import admin
from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from aircraft.views import AircraftListView
from core.views import health_check
from ingestion.views import IngestView
from reports.views import ReportViewSet
from taxonomy.views import LocationListView, TaxonomyListView
from users.views import AppTokenObtainPairView

router = DefaultRouter()
router.register("reports", ReportViewSet, basename="reports")

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/health/", health_check),
    path("api/v1/auth/token/", AppTokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/v1/auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("api/v1/ingest/", IngestView.as_view(), name="ingest"),
    path("api/v1/taxonomy/", TaxonomyListView.as_view(), name="taxonomy-list"),
    path("api/v1/locations/", LocationListView.as_view(), name="locations-list"),
    path("api/v1/aircraft/", AircraftListView.as_view(), name="aircraft-list"),
    path("api/v1/", include(router.urls)),
]
