from django.db import transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import IngestPayloadSerializer
from .storage import upload_json_payload
from .tasks import parse_feed_task


class IngestView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        s3_blob_key = upload_json_payload(request.data.get("payload", {}))
        serializer = IngestPayloadSerializer(
            data=request.data,
            context={
                "request": request,
                "s3_blob_key": s3_blob_key,
                "idempotency_key": request.headers.get("Idempotency-Key"),
            },
        )
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            payload = serializer.save()
        parse_feed_task.delay(str(payload.id))
        return Response({"id": payload.id, "status": payload.processing_status}, status=status.HTTP_202_ACCEPTED)
