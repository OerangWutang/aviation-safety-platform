from django.db import models

from core.models import TimestampedModel


class Organization(TimestampedModel):
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)

    def __str__(self):
        return self.name
