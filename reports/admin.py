from django.contrib import admin
from .models import Report, ReportReadModel

@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ["tracking_number", "organization", "status", "version", "event_date", "created_at"]
    list_filter = ["status", "organization"]
    search_fields = ["tracking_number", "narrative"]
    readonly_fields = ["tracking_number", "version", "created_at", "updated_at"]

@admin.register(ReportReadModel)
class ReportReadModelAdmin(admin.ModelAdmin):
    list_display = ["report", "document_version", "refreshed_at"]
    readonly_fields = ["report", "document", "document_version", "refreshed_at"]
