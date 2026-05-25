from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import anyio
import orjson
from fastapi import Response
from fastapi.encoders import jsonable_encoder

_ORJSON_OPTIONS = orjson.OPT_NON_STR_KEYS | orjson.OPT_SERIALIZE_NUMPY


def render_json_bytes(content: Any) -> bytes:
    """Render API content with FastAPI-compatible coercion and orjson speed.

    ``jsonable_encoder`` normalises Pydantic models, dataclasses, UUIDs, and
    datetimes to JSON-compatible objects. ``orjson`` then performs the final
    bytes render substantially faster than the standard library encoder.
    """
    return orjson.dumps(jsonable_encoder(content), option=_ORJSON_OPTIONS)


async def offloaded_json_response(
    content: Any,
    *,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
) -> Response:
    """Serialize a potentially large JSON response off the ASGI event loop.

    Use this only for high-cardinality read endpoints where JSON encoding can
    take long enough to starve other coroutines. Small responses should rely on
    FastAPI's default ``ORJSONResponse`` path to avoid unnecessary thread hops.
    """
    body = await anyio.to_thread.run_sync(render_json_bytes, content)
    return Response(
        content=body,
        status_code=status_code,
        headers=dict(headers or {}),
        media_type="application/json",
    )
