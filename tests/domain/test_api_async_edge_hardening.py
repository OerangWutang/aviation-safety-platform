from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text()


def test_app_uses_orjson_for_large_payload_offload() -> None:
    """orjson is used for large-payload serialisation via thread-pool offload.

    FastAPI deprecated ``ORJSONResponse`` as a response class — it now
    serialises Pydantic models natively.  The fast JSON path that matters here
    is ``offloaded_json_response`` in ``responses.py``, which runs
    ``orjson.dumps`` in a thread pool so CPU-intensive serialisation of large
    accident / provenance payloads does not block the event loop.
    """
    responses_source = _read("src/atlas/presentation/api/responses.py")
    middleware_source = _read("src/atlas/presentation/api/middleware.py")
    requirements = _read("requirements.in")
    app_source = _read("src/atlas/presentation/api/app.py")

    assert "anyio.to_thread.run_sync" in responses_source
    assert "orjson.dumps" in responses_source
    assert "import orjson" in middleware_source
    assert "orjson>=3.10" in requirements
    assert "ORJSONResponse" not in app_source, (
        "ORJSONResponse is deprecated in this FastAPI version. "
        "Use JSONResponse for error handlers and Pydantic-native serialisation "
        "for route responses.  Large payloads use offloaded_json_response()."
    )


def test_large_read_endpoints_offload_json_rendering() -> None:
    responses_source = _read("src/atlas/presentation/api/responses.py")
    accidents_source = _read("src/atlas/presentation/api/routers/accidents.py")
    provenance_source = _read("src/atlas/presentation/api/routers/provenance.py")

    assert "anyio.to_thread.run_sync" in responses_source
    assert "orjson.dumps" in responses_source
    assert "return await offloaded_json_response(payload)" in accidents_source
    assert "return await offloaded_json_response(payload)" in provenance_source
    assert "await uow.rollback()" in provenance_source


def test_uow_dependency_closes_before_response_serialization() -> None:
    for path in Path(ROOT / "src/atlas/presentation/api/routers").glob("*.py"):
        source = path.read_text()
        if "Depends(get_uow" not in source:
            continue
        assert 'Depends(get_uow, scope="function")' in source, path

    dependency_source = _read("src/atlas/presentation/api/dependencies.py")
    assert "if session.in_transaction():" in dependency_source
    assert "await uow.rollback()" in dependency_source


def test_auth_uses_short_lived_cached_lookup_not_request_uow_session() -> None:
    dependency_source = _read("src/atlas/presentation/api/dependencies.py")
    config_source = _read("src/atlas/config.py")

    assert "Depends(get_session)" not in dependency_source
    assert (
        "from atlas.infrastructure.db.session import (\n    async_public_session_factory,\n    async_session_factory,"
        in dependency_source
        or "from atlas.infrastructure.db.session import async_session_factory" in dependency_source
    )
    assert "_AUTH_CACHE" in dependency_source
    assert "api_key_cache_ttl_seconds" in config_source
    assert "api_key_cache_max_entries" in config_source
    assert "_AUTH_CACHE.popitem(last=False)" in dependency_source


def test_session_can_delegate_pooling_to_pgbouncer_nullpool() -> None:
    session_source = _read("src/atlas/infrastructure/db/session.py")
    config_source = _read("src/atlas/config.py")

    assert "from sqlalchemy.pool import NullPool" in session_source
    assert "db_use_null_pool" in config_source
    assert 'engine_kwargs["poolclass"] = NullPool' in session_source
    assert "pool_size=settings.db_pool_size" in session_source
