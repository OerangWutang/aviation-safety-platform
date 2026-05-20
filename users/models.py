from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models

from core.models import TimestampedModel


class AppUserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Email is required")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("role", AppUser.Role.ADMIN)
        return self.create_user(email, password, **extra_fields)


class AppUser(TimestampedModel, AbstractBaseUser, PermissionsMixin):
    class Role(models.TextChoices):
        ANALYST = "analyst", "Analyst"
        SAFETY_OFFICER = "safety_officer", "Safety Officer"
        ADMIN = "admin", "Admin"

    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="users")
    email = models.EmailField(unique=True)
    role = models.CharField(max_length=32, choices=Role.choices)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["organization"]

    objects = AppUserManager()

    def __str__(self):
        return self.email
