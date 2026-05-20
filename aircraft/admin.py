from django.contrib import admin

from .models import Aircraft, AircraftRegistration, ReportAircraft

admin.site.register(Aircraft)
admin.site.register(AircraftRegistration)
admin.site.register(ReportAircraft)
