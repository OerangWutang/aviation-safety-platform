import uuid
import django.db.models.deletion
from django.db import migrations, models

class Migration(migrations.Migration):
    initial = True
    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("organizations", "0001_initial"),
    ]
    operations = [
        migrations.CreateModel(
            name="AppUser",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("password", models.CharField(max_length=128, verbose_name="password")),
                ("last_login", models.DateTimeField(blank=True, null=True, verbose_name="last login")),
                ("is_superuser", models.BooleanField(default=False)),
                ("email", models.EmailField(max_length=254, unique=True)),
                ("role", models.CharField(
                    choices=[("analyst","Analyst"),("safety_officer","Safety Officer"),("admin","Admin")],
                    default="analyst", max_length=20,
                )),
                ("is_active", models.BooleanField(default=True)),
                ("is_staff", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("organization", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="members",
                    to="organizations.organization",
                )),
                ("groups", models.ManyToManyField(blank=True, related_name="users_appuser_groups", to="auth.group")),
                ("user_permissions", models.ManyToManyField(blank=True, related_name="users_appuser_permissions", to="auth.permission")),
            ],
            options={"ordering": ["email"], "abstract": False},
        ),
    ]
