from django.contrib import admin

from .models import LocationNode, TaxonomyNode

admin.site.register(TaxonomyNode)
admin.site.register(LocationNode)
