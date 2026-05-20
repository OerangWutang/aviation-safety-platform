import pytest
from reports.models import Report, ReportStatus


@pytest.mark.django_db
def test_create_report(analyst_client, analyst):
    response = analyst_client.post(
        "/api/v1/reports/",
        {"event_date": "2026-05-01", "narrative": "Bird strike on final approach."},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["status"] == ReportStatus.DRAFT
    assert data["version"] == 1
    assert data["tracking_number"].startswith("ASP-")


@pytest.mark.django_db
def test_list_reports_scoped_to_tenant(analyst_client, analyst, org, db):
    from organizations.models import Organization
    from users.models import AppUser, Role

    Report.objects.create(organization=org, created_by=analyst)

    other_org = Organization.objects.create(name="Other Org", slug="other-org")
    other_user = AppUser.objects.create_user(
        email="other@other.org", organization=other_org, password="pass", role=Role.ANALYST,
    )
    Report.objects.create(organization=other_org, created_by=other_user)

    response = analyst_client.get("/api/v1/reports/")
    assert response.status_code == 200
    result_ids = [r["id"] for r in response.json()["results"]]
    assert all(
        str(Report.objects.get(id=rid).organization_id) == str(org.id)
        for rid in result_ids
    )


@pytest.mark.django_db
def test_report_status_transition(analyst_client, analyst, org):
    report = Report.objects.create(organization=org, created_by=analyst)
    response = analyst_client.post(
        f"/api/v1/reports/{report.id}/transition/",
        {"status": ReportStatus.UNDER_REVIEW},
    )
    assert response.status_code == 200
    report.refresh_from_db()
    assert report.status == ReportStatus.UNDER_REVIEW
    assert report.version == 2


@pytest.mark.django_db
def test_invalid_status_transition_rejected(analyst_client, analyst, org):
    report = Report.objects.create(organization=org, created_by=analyst)
    response = analyst_client.post(
        f"/api/v1/reports/{report.id}/transition/",
        {"status": ReportStatus.REJECTED},
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_report_detail_cannot_update_status_directly(analyst_client, analyst, org):
    report = Report.objects.create(organization=org, created_by=analyst)
    response = analyst_client.patch(
        f"/api/v1/reports/{report.id}/",
        {"status": ReportStatus.UNDER_REVIEW, "narrative": "Updated narrative"},
    )
    assert response.status_code == 200
    report.refresh_from_db()
    assert report.status == ReportStatus.DRAFT
    assert report.version == 1
    assert report.narrative == "Updated narrative"


@pytest.mark.django_db
def test_cross_tenant_report_not_accessible(api_client, db):
    from organizations.models import Organization
    from users.models import AppUser, Role
    from rest_framework_simplejwt.tokens import RefreshToken

    org_a = Organization.objects.create(name="Org A", slug="org-a")
    org_b = Organization.objects.create(name="Org B", slug="org-b")
    user_a = AppUser.objects.create_user(email="a@org.com", organization=org_a, password="pass")
    user_b = AppUser.objects.create_user(email="b@org.com", organization=org_b, password="pass")
    report_b = Report.objects.create(organization=org_b, created_by=user_b)

    token = RefreshToken.for_user(user_a)
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
    response = api_client.get(f"/api/v1/reports/{report_b.id}/")
    assert response.status_code == 404
