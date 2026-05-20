from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import AppUser

@admin.register(AppUser)
class AppUserAdmin(UserAdmin):
    model = AppUser
    list_display = ["email", "organization", "role", "is_active", "created_at"]
    list_filter = ["role", "is_active", "organization"]
    search_fields = ["email"]
    ordering = ["email"]
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("organization", "role")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("email", "organization", "role", "password1", "password2")}),
    )
