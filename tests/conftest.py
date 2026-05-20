import pytest
from organizations.models import Organization
from users.models import AppUser, Role


@pytest.fixture
def org(db):
    return Organization.objects.create(name="Test Org", slug="test-org")


@pytest.fixture
def analyst(db, org):
    return AppUser.objects.create_user(
        email="analyst@test.org",
        organization=org,
        password="testpass123",
        role=Role.ANALYST,
    )


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient
    return APIClient()


@pytest.fixture
def analyst_client(api_client, analyst):
    from rest_framework_simplejwt.tokens import RefreshToken
    token = RefreshToken.for_user(analyst)
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
    return api_client
