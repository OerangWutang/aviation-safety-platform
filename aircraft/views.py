from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated

from .models import Aircraft
from .serializers import AircraftSerializer


class AircraftListView(ListAPIView):
    serializer_class = AircraftSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        org_id = self.request.organization_id
        return Aircraft.objects.filter(organization_id__in=[org_id, None])
