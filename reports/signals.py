from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from ingestion.models import OutboxEvent

from .models import Report


@receiver(pre_save, sender=Report)
def cache_previous_status(sender, instance, **kwargs):
    if not instance.pk:
        instance._previous_status = None
        return
    try:
        instance._previous_status = Report.objects.get(pk=instance.pk).status
    except Report.DoesNotExist:
        instance._previous_status = None


@receiver(post_save, sender=Report)
def create_status_change_outbox(sender, instance, created, **kwargs):
    prev = getattr(instance, "_previous_status", None)
    if created or prev == instance.status:
        return

    OutboxEvent.objects.create(
        organization=instance.organization,
        aggregate_id=instance.id,
        aggregate_type="report",
        event_type="report.status_changed",
        payload={
            "report_id": str(instance.id),
            "from_status": prev,
            "to_status": instance.status,
        },
        available_at=timezone.now(),
    )
