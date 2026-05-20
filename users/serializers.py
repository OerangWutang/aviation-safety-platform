from rest_framework_simplejwt.serializers import TokenObtainPairSerializer


class AppTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["organization_id"] = str(user.organization_id)
        token["role"] = user.role
        return token
