from django.db import models

from core.models import TimestampedModel


class Aircraft(TimestampedModel):
    class AircraftCategory(models.TextChoices):
        AIRPLANE = "airplane", "Airplane"
        HELICOPTER = "helicopter", "Helicopter"
        GLIDER = "glider", "Glider"
        BALLOON = "balloon", "Balloon"
        OTHER = "other", "Other"

    make = models.CharField(max_length=100)
    model = models.CharField(max_length=100)
    aircraft_category = models.CharField(max_length=20, choices=AircraftCategory.choices)
    serial_number = models.CharField(max_length=100)
    is_global_reference = models.BooleanField(default=False)
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="aircraft", null=True, blank=True
    )

    def __str__(self):
        return f"{self.make} {self.model}"


class AircraftRegistration(TimestampedModel):
    aircraft = models.ForeignKey(Aircraft, on_delete=models.CASCADE, related_name="registrations")
    registration_number = models.CharField(max_length=32)
    country_code = models.CharField(max_length=2)
    valid_from = models.DateField()
    valid_to = models.DateField(null=True, blank=True)
    is_current = models.BooleanField(default=True)

    def __str__(self):
        return self.registration_number


class ReportAircraft(models.Model):
    class Role(models.TextChoices):
        PRIMARY = "primary", "Primary"
        SECONDARY = "secondary", "Secondary"
        OTHER = "other", "Other"

    class Damage(models.TextChoices):
        NONE = "none", "None"
        MINOR = "minor", "Minor"
        SUBSTANTIAL = "substantial", "Substantial"
        DESTROYED = "destroyed", "Destroyed"

    report = models.ForeignKey("reports.Report", on_delete=models.CASCADE)
    aircraft = models.ForeignKey(Aircraft, on_delete=models.CASCADE)
    role = models.CharField(max_length=16, choices=Role.choices)
    damage = models.CharField(max_length=16, choices=Damage.choices)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["report", "aircraft"], name="uniq_report_aircraft")]

    def __str__(self):
        return f"{self.report_id}:{self.aircraft_id}:{self.role}"
