import pytest
from django.contrib import admin

from users.admin import AppUserAdmin, AppUserChangeForm, AppUserCreationForm
from users.models import AppUser, Role


@pytest.mark.django_db
def test_app_user_admin_uses_custom_forms():
    app_user_admin = AppUserAdmin(AppUser, admin.site)

    assert app_user_admin.add_form is AppUserCreationForm
    assert app_user_admin.form is AppUserChangeForm


@pytest.mark.django_db
def test_app_user_creation_form_supports_required_custom_fields(org):
    form = AppUserCreationForm(
        data={
            "email": "admin@test.org",
            "organization": org.pk,
            "role": Role.ADMIN,
            "password1": "safe-test-pass123",
            "password2": "safe-test-pass123",
        }
    )

    assert form.is_valid(), form.errors


@pytest.mark.django_db
def test_app_user_change_form_exposes_custom_fields(org):
    user = AppUser.objects.create_user(
        email="analyst-change@test.org",
        organization=org,
        password="safe-test-pass123",
        role=Role.ANALYST,
    )

    form = AppUserChangeForm(instance=user)

    assert "organization" in form.fields
    assert "role" in form.fields
