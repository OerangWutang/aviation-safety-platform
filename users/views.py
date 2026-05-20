from rest_framework_simplejwt.views import TokenObtainPairView

from .serializers import AppTokenObtainPairSerializer


class AppTokenObtainPairView(TokenObtainPairView):
    serializer_class = AppTokenObtainPairSerializer
