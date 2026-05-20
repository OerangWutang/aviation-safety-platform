import pytest
from rest_framework.test import APIClient

from organizations.models import Organization
from users.models import AppUser


@pytest.mark.django_db
def test_create_report_authenticated():
    org = Organization.objects.create(name='Org One', slug='org-one')
    user = AppUser.objects.create_user(
        email='analyst@example.com',
        password='testpass123',
        organization=org,
        role=AppUser.Role.ANALYST,
    )

    client = APIClient()
    token_resp = client.post('/api/v1/auth/token/', {'email': user.email, 'password': 'testpass123'}, format='json')
    assert token_resp.status_code == 200
    access = token_resp.json()['access']

    client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
    resp = client.post(
        '/api/v1/reports/',
        {
            'tracking_number': 'RPT-1001',
            'external_source_id': 'ext-1001',
            'source_url': 'https://example.com/report/1001',
            'narrative': 'Bird strike on approach.',
            'status': 'draft',
        },
        format='json',
    )
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body['tracking_number'] == 'RPT-1001'
    assert body['organization'] == str(org.id)
