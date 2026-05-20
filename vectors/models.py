from django.db import models
from pgvector.django import VectorField

from core.models import TimestampedModel


class ReportVector(TimestampedModel):
    report = models.ForeignKey("reports.Report", on_delete=models.CASCADE, related_name="vectors")
    report_version = models.IntegerField()
    chunk_index = models.IntegerField()
    chunk_hash = models.CharField(max_length=64, unique=True)
    chunk_text = models.TextField()
    embedding_model = models.CharField(max_length=100)
    embedding_dimensions = models.IntegerField()
    embedding = VectorField(dimensions=1536)
    embedded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.report_id}:{self.chunk_index}"
