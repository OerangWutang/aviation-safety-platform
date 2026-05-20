from rest_framework.permissions import BasePermission, SAFE_METHODS

class IsTenantMember(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        org_id = getattr(obj, "organization_id", None)
        return org_id is not None and str(org_id) == str(request.user.organization_id)

class IsAdminOrSafetyOfficer(IsTenantMember):
    WRITE_ROLES = {"admin", "safety_officer"}
    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        if request.method in SAFE_METHODS:
            return True
        return request.user.role in self.WRITE_ROLES
