from rest_framework.permissions import BasePermission


class IsTenantScoped(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and getattr(request, "organization_id", None))

    def has_object_permission(self, request, view, obj):
        request_org_id = getattr(request, "organization_id", None)
        if not request_org_id:
            return False
        obj_org_id = getattr(obj, "organization_id", None)
        if obj_org_id is None and hasattr(obj, "report"):
            obj_org_id = getattr(obj.report, "organization_id", None)
        return str(obj_org_id) == str(request_org_id)
