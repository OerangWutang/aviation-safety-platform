from django.contrib import admin

from .models import OrgReportsReadModel, Report, ReportAttachment, ReportReadModel, ReportReview

admin.site.register(Report)
admin.site.register(ReportReview)
admin.site.register(ReportAttachment)
admin.site.register(ReportReadModel)
admin.site.register(OrgReportsReadModel)
