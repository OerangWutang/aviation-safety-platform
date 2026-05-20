import hashlib

from celery import shared_task

from reports.models import Report

from .models import ReportVector


@shared_task
def generate_embeddings_task(report_id):
    report = Report.objects.get(pk=report_id)
    text = report.narrative or ""
    chunks = [text[i : i + 500] for i in range(0, len(text), 500)] or [""]
    vectors = []
    for idx, chunk in enumerate(chunks):
        chunk_hash = hashlib.sha256(f"{report.id}:{report.version}:{idx}:{chunk}".encode()).hexdigest()
        embedding = [0.0] * 1536
        vectors.append(
            ReportVector(
                report=report,
                report_version=report.version,
                chunk_index=idx,
                chunk_hash=chunk_hash,
                chunk_text=chunk,
                embedding_model="stub-local",
                embedding_dimensions=1536,
                embedding=embedding,
            )
        )
    ReportVector.objects.bulk_create(vectors, ignore_conflicts=True)
