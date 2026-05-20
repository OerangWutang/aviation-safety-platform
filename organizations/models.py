from django.db import models
from core.models import TimestampedModel

class Organization(TimestampedModel):
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=100, unique=True)
    class Meta:
        ordering = ["name"]
    def __str__(self):
        return self.name
