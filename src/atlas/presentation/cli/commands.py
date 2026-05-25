from __future__ import annotations

import asyncio
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import typer

from atlas.application.dto import IngestionClaimDTO
from atlas.application.use_cases.ingest_source_data import IngestSourceData
from atlas.application.use_cases.query_conflict_history import QueryConflictHistory
from atlas.application.use_cases.rebuild_all_projections import RebuildAllProjections
from atlas.application.use_cases.reproject_event import ReProjectEvent
from atlas.application.use_cases.run_hermes_fetch_job import RunHermesFetchJob
from atlas.config import get_settings
from atlas.domain.entities import Source
from atlas.domain.enums import Role, SourceKind
from atlas.infrastructure.db.orm_models import ApiKeyModel
from atlas.infrastructure.db.session import async_session_factory
from atlas.infrastructure.db.unit_of_work import create_uow
from atlas.infrastructure.event_bus.outbox_worker import OutboxWorker
from atlas.logging_config import setup_logging
from atlas.security import hash_api_key

app = typer.Typer(no_args_is_help=True)


def _setup() -> None:
    """Call once per command entry-point, not at import time."""
    setup_logging()


@app.command("ingest")
def ingest(
    source_id: UUID,
    file: Path | None = typer.Option(None, help="JSON file with raw_payload and claims"),
    raw: str | None = typer.Option(None, help="Inline raw payload JSON"),
    claims: str | None = typer.Option(None, help="Inline claims JSON array"),
    event_id: UUID | None = typer.Option(None, help="Existing event ID to ingest into"),
):
    _setup()

    async def run() -> None:
        if file:
            data = json.loads(file.read_text())
            raw_payload = data.get("raw_payload", data.get("raw", data))
            claims_data = data.get("claims", [])
        else:
            raw_payload = json.loads(raw or "{}")
            claims_data = json.loads(claims or "[]")
        async with create_uow() as uow:
            eid = await IngestSourceData(uow).execute(
                source_id=source_id,
                raw_payload=raw_payload,
                ingestion_run_id=uuid4(),
                claims_data=[IngestionClaimDTO(**item) for item in claims_data],
                event_id=event_id,
            )
            typer.echo(f"Ingestion successful. Event ID: {eid}")

    asyncio.run(run())


@app.command("projections-rebuild")
def projections_rebuild(
    event_id: UUID | None = typer.Option(None),
    all: bool = typer.Option(False, "--all"),
):
    _setup()

    async def run() -> None:
        async with create_uow() as uow:
            if all:
                result = await RebuildAllProjections(uow).execute()
                typer.echo(f"Rebuilt {result.processed} projections ({result.skipped} skipped)")
            elif event_id:
                await ReProjectEvent(uow).execute(event_id)
                typer.echo(f"Rebuilt projection for {event_id}")
            else:
                raise typer.BadParameter("Use --event-id or --all")

    asyncio.run(run())


@app.command("outbox-process")
def outbox_process(limit: int = typer.Option(100)):
    _setup()

    async def run() -> None:
        processed = await OutboxWorker(worker_id="cli").process_batch(limit=limit)
        typer.echo(f"Processed {processed} outbox events")

    asyncio.run(run())


@app.command("outbox-worker")
def outbox_worker(sleep_seconds: float = typer.Option(5.0)):
    _setup()
    asyncio.run(OutboxWorker(worker_id="cli-worker").run_loop(sleep_seconds=sleep_seconds))


@app.command("hermes-worker")
def hermes_worker(
    sleep_seconds: float = typer.Option(5.0, help="Seconds to sleep when no jobs are due"),
    batch_limit: int = typer.Option(1, min=1, max=100, help="Jobs to claim per polling cycle"),
    lease_seconds: int = typer.Option(300, min=30, max=3600, help="Claim lease duration"),
    recover_limit: int = typer.Option(
        100, min=1, max=1000, help="Expired RUNNING jobs to recover per cycle"
    ),
    once: bool = typer.Option(False, "--once", help="Process one polling cycle and exit"),
):
    """Run the Hermes fetch queue worker.

    The worker first recovers expired RUNNING leases, then atomically claims due
    QUEUED jobs using claim_next_running().  Each job is finalized with lease
    fencing so stale workers cannot overwrite recovered claims.

    Recovery audit trail:  when a recovered job has exhausted its retry budget
    the worker emits a ``FETCH_FAILED`` ``HermesSourceChange`` so the failure
    surfaces in the target's change stream, not only in the job record.
    Requeued recoveries do not emit a change event because the next run will
    produce one if it also fails.
    """
    from atlas.domain.entities import HermesSourceChange
    from atlas.domain.enums import HermesChangeType
    from atlas.domain.enums import HermesFetchJobStatus as _Status

    _setup()
    settings = get_settings()
    settings.validate_hermes_worker_settings()
    _allowed_hosts = tuple(settings.hermes_allowed_hosts)

    async def run() -> None:
        worker_prefix = f"hermes-worker:{uuid4()}"
        while True:
            processed = 0

            async with create_uow() as uow:
                outcomes = await uow.hermes_fetch_jobs.recover_stale_running(
                    now=datetime.now(UTC),
                    limit=recover_limit,
                )
                if outcomes:
                    now = datetime.now(UTC)
                    terminal = [o for o in outcomes if o.final_status == _Status.FAILED]
                    for outcome in terminal:
                        # One FETCH_FAILED change per terminally-failed
                        # recovery; the job record's error_message still
                        # carries the lease-expiry reason.
                        await uow.hermes_source_changes.add(
                            HermesSourceChange(
                                target_id=outcome.target_id,
                                fetch_job_id=outcome.job_id,
                                change_type=HermesChangeType.FETCH_FAILED,
                                detected_at=now,
                            )
                        )
                    await uow.commit()
                    typer.echo(
                        f"Recovered {len(outcomes)} stale Hermes jobs "
                        f"({len(terminal)} terminal, {len(outcomes) - len(terminal)} requeued)"
                    )

            for _ in range(batch_limit):
                async with create_uow() as uow:
                    worker_id = f"{worker_prefix}:{uuid4()}"
                    job = await uow.hermes_fetch_jobs.claim_next_running(
                        worker_id=worker_id,
                        lease_expires_at=datetime.now(UTC) + timedelta(seconds=lease_seconds),
                    )
                    if job is None:
                        break
                    result = await RunHermesFetchJob(
                        uow,
                        worker_id_prefix=worker_prefix,
                        lease_seconds=lease_seconds,
                        allowed_hosts=_allowed_hosts,
                    ).execute_claimed(job)
                    processed += 1
                    typer.echo(f"Hermes job {result.job_id} -> {result.status.value}")

            if once:
                break
            if processed == 0:
                await asyncio.sleep(sleep_seconds)

    asyncio.run(run())


@app.command("conflicts-history")
def conflicts_history(conflict_id: UUID):
    _setup()

    async def run() -> None:
        async with create_uow() as uow:
            result = await QueryConflictHistory(uow).execute(conflict_id)
            typer.echo(json.dumps(result, default=str, indent=2))

    asyncio.run(run())


@app.command("bootstrap")
def bootstrap(
    role: str = typer.Option(
        "admin", help=f"Role for the generated API key. One of: {', '.join(Role.values())}"
    ),
    api_key: str | None = typer.Option(None, help="Optional plain API key to hash and store"),
):
    """Create the CuratorOverride source and a development API key.

    Safe to run multiple times: the source insert is idempotent (ON CONFLICT
    DO NOTHING) and the key is always a fresh UUID.
    """
    _setup()

    # Validate role before any async work so the error surfaces immediately
    # with a clear message rather than a constraint violation from Postgres.
    if role not in Role.values():
        typer.echo(
            f"Invalid role {role!r}. Must be one of: {', '.join(sorted(Role.values()))}",
            err=True,
        )
        raise typer.Exit(code=2)

    async def run() -> None:
        plain_key = api_key or secrets.token_urlsafe(32)
        key_hash = hash_api_key(plain_key)
        user_id = uuid4()

        # Step 1 - seed the CuratorOverride source (own transaction).
        async with create_uow() as uow:
            settings = get_settings()
            existing = await uow.sources.get(settings.curator_override_source_id)
            if not existing:
                await uow.sources.add(
                    Source(
                        id=settings.curator_override_source_id,
                        name=settings.curator_override_source_name,
                        kind=SourceKind.INTERNAL,
                        reliability_tier=1,
                    )
                )
            await uow.commit()

        # Step 2 - create the API key (separate transaction so a key failure
        # does not roll back the source seed).
        async with async_session_factory() as session:
            try:
                session.add(ApiKeyModel(id=uuid4(), key_hash=key_hash, user_id=user_id, role=role))
                await session.commit()
            except Exception as exc:
                await session.rollback()
                typer.echo(f"Failed to create API key: {exc}", err=True)
                raise typer.Exit(code=1) from exc

        typer.echo("Bootstrap complete.")
        typer.echo(f"User ID:  {user_id}")
        typer.echo(f"API key:  {plain_key}")
        typer.echo("Store this key securely; only its hash was saved.")

    asyncio.run(run())
