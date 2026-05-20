from rest_framework.permissions import BasePermission
from organizations.models import OrganizationMembership


class IsOrganizationMember(BasePermission):
    """Allow access only to members of the organization."""

    def has_permission(self, request, view):
        org_slug = view.kwargs.get('org_slug')
        if not org_slug:
            return False
        return OrganizationMembership.objects.filter(
            user=request.user,
            organization__slug=org_slug,
            is_active=True
        ).exists()


class IsOrganizationAdmin(BasePermission):
    """Allow access only to admins of the organization."""

    def has_permission(self, request, view):
        org_slug = view.kwargs.get('org_slug')
        if not org_slug:
            return False
        return OrganizationMembership.objects.filter(
            user=request.user,
            organization__slug=org_slug,
            role='admin',
            is_active=True
        ).exists()
