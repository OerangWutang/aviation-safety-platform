from django.db import models

from core.models import TimestampedModel


class TaxonomyNode(TimestampedModel):
    class TaxonomyType(models.TextChoices):
        EVENT_TYPE = "event_type", "Event Type"
        CATEGORY = "category", "Category"

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="taxonomy_nodes", null=True, blank=True
    )
    parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True, related_name="children")
    label = models.CharField(max_length=255)
    path = models.CharField(max_length=500, unique=True)
    taxonomy_type = models.CharField(max_length=20, choices=TaxonomyType.choices)
    is_active = models.BooleanField(default=True)
    is_global_reference = models.BooleanField(default=False)

    def __str__(self):
        return self.label


class LocationNode(TimestampedModel):
    class LocationType(models.TextChoices):
        REGION = "region", "Region"
        COUNTRY = "country", "Country"
        CITY = "city", "City"
        AIRPORT = "airport", "Airport"
        RUNWAY = "runway", "Runway"
        RAMP = "ramp", "Ramp"

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="location_nodes", null=True, blank=True
    )
    parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True, related_name="children")
    label = models.CharField(max_length=255)
    path = models.CharField(max_length=500, unique=True)
    location_type = models.CharField(max_length=20, choices=LocationType.choices)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    point = models.JSONField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    is_global_reference = models.BooleanField(default=False)

    def __str__(self):
        return self.label
