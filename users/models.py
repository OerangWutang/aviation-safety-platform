import uuid
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models


class Role(models.TextChoices):
    ANALYST = "analyst", "Analyst"
    SAFETY_OFFICER = "safety_officer", "Safety Officer"
    ADMIN = "admin", "Admin"


class AppUserManager(BaseUserManager):
    def create_user(self, email, organization, password=None, **extra_fields):
        if not email:
            raise ValueError("Email is required")
        if not organization:
            raise ValueError("Organization is required")
        email = self.normalize_email(email)
        extra_fields.setdefault("role", Role.ANALYST)
        extra_fields.setdefault("is_active", True)
        user = self.model(email=email, organization=organization, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        from organizations.models import Organization
        org, _ = Organization.objects.get_or_create(slug="system", defaults={"name": "System"})
        extra_fields.setdefault("role", Role.ADMIN)
        extra_fields.setdefault("is_active", True)
        if extra_fields.get("is_staff") is False:
            raise ValueError("Superuser must have is_staff=True")
        if extra_fields.get("is_superuser") is False:
            raise ValueError("Superuser must have is_superuser=True")
        extra_fields["is_staff"] = True
        extra_fields["is_superuser"] = True
        return self.create_user(email, org, password, **extra_fields)


class AppUser(AbstractBaseUser, PermissionsMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="members",
    )
    email = models.EmailField(unique=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.ANALYST)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = AppUserManager()
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        ordering = ["email"]

    def __str__(self):
        return f"{self.email} ({self.role})"
