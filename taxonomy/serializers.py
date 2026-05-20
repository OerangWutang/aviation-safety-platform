from rest_framework import serializers

from .models import LocationNode, TaxonomyNode


class TaxonomyNodeSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaxonomyNode
        fields = "__all__"


class LocationNodeSerializer(serializers.ModelSerializer):
    class Meta:
        model = LocationNode
        fields = "__all__"
