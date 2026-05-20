from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated

from .models import LocationNode, TaxonomyNode
from .serializers import LocationNodeSerializer, TaxonomyNodeSerializer


class TaxonomyListView(ListAPIView):
    serializer_class = TaxonomyNodeSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        org_id = self.request.organization_id
        return TaxonomyNode.objects.filter(is_active=True).filter(organization_id__in=[org_id, None])


class LocationListView(ListAPIView):
    serializer_class = LocationNodeSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        org_id = self.request.organization_id
        return LocationNode.objects.filter(is_active=True).filter(organization_id__in=[org_id, None])
