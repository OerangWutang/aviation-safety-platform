from django.db import models
from core.models import TimeStampedModel

class Organization(TimeStampedModel):
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=120, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name
