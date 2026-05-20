import jwt
from rest_framework_simplejwt.tokens import UntypedToken


class OrganizationMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.organization_id = None
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1]
            try:
                UntypedToken(token)
                data = jwt.decode(token, options={"verify_signature": False}, algorithms=["HS256"])
                request.organization_id = data.get("organization_id")
            except Exception:
                request.organization_id = None

        if getattr(request, "user", None) and request.user.is_authenticated and not request.organization_id:
            request.organization_id = str(request.user.organization_id)

        return self.get_response(request)
