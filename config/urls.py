from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('reports.urls')),
    path('api/', include('organizations.urls', namespace='organizations')),
    path('', include('core.urls')),
]
