import uuid
import django.db.models.deletion
import django.utils.timezone
import reports.models
from django.conf import settings
from django.db import migrations, models

class Migration(migrations.Migration):
    initial = True
    dependencies = [
        ("organizations", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name="Report",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="reports", to="organizations.organization")),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="created_reports", to=settings.AUTH_USER_MODEL)),
                ("tracking_number", models.CharField(default=reports.models._generate_tracking_number, editable=False, max_length=32, unique=True)),
                ("event_date", models.DateField(blank=True, null=True)),
                ("narrative", models.TextField(blank=True, default="")),
                ("status", models.CharField(
                    choices=[
                        ("draft","Draft"),("ingested","Ingested"),("validation_failed","Validation Failed"),
                        ("under_review","Under Review"),("requires_revision","Requires Revision"),
                        ("approved_queued","Approved \u2013 Post-processing Queued"),
                        ("published","Published"),("rejected","Rejected"),("archived","Archived"),
                    ],
                    db_index=True, default="draft", max_length=30,
                )),
                ("version", models.PositiveIntegerField(default=1)),
            ],
            options={"ordering": ["-created_at"], "abstract": False},
        ),
        migrations.AddIndex(
            model_name="report",
            index=models.Index(fields=["organization", "status"], name="reports_org_status_idx"),
        ),
        migrations.AddIndex(
            model_name="report",
            index=models.Index(fields=["organization", "-created_at"], name="reports_org_created_idx"),
        ),
        migrations.CreateModel(
            name="ReportReadModel",
            fields=[
                ("report", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, primary_key=True, related_name="read_model", serialize=False, to="reports.report")),
                ("document", models.JSONField(default=dict)),
                ("document_version", models.PositiveIntegerField(default=1)),
                ("refreshed_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={"verbose_name": "Report read model"},
        ),
    ]
