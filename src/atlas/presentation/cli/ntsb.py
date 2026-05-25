"""CLI for the NTSB eADMS bulk importer.

Three subcommands, in the order you'd use them:

    # 1. one-time: pull the tables out of avall.mdb into CSVs
    python -m atlas.presentation.cli.ntsb export --mdb avall.mdb --out ./ntsb_csv

    # 2. verify the mapping without any database (writes JSONL submissions)
    python -m atlas.presentation.cli.ntsb dry-run --csv ./ntsb_csv --out subs.jsonl --limit 100

    # 3. load into Atlas via the real IngestSourceData use case (needs DB)
    python -m atlas.presentation.cli.ntsb load --csv ./ntsb_csv

``load`` is the only DB-touching path; its database imports are deferred so
``export`` and ``dry-run`` run with nothing but mdbtools / the stdlib + pydantic.

Idempotency makes ``load`` safely resumable: re-running replays already-ingested
records cheaply and only writes genuinely new/changed ones.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer

from atlas.application.ingestion.sources.ntsb_eadms import (
    NTSB_FIELD_MAPPING,
    NTSB_RELIABILITY_TIER,
    NTSB_SOURCE_NAME,
)
from atlas.infrastructure.ingestion import NtsbEadmsReader, export_mdb_tables

app = typer.Typer(no_args_is_help=True, help="NTSB eADMS (avall.mdb) importer.")


@app.command("export")
def export(
    mdb: Path = typer.Option(..., help="Path to avall.mdb"),
    out: Path = typer.Option(..., help="Directory to write per-table CSVs into"),
) -> None:
    """Export the needed eADMS tables from avall.mdb to CSV (requires mdbtools)."""
    written = export_mdb_tables(mdb, out)
    for table, path in written.items():
        typer.echo(f"  {table:28s} -> {path}")
    typer.echo(f"Exported {len(written)} tables to {out}")


@app.command("dry-run")
def dry_run(
    csv: Path = typer.Option(..., help="Directory of exported eADMS CSVs"),
    out: Path | None = typer.Option(None, help="Write submissions as JSONL here"),
    limit: int = typer.Option(0, help="Stop after N records (0 = all)"),
) -> None:
    """Map records and emit Atlas submissions as JSONL. No database is touched."""
    reader = NtsbEadmsReader(csv)
    sink = out.open("w", encoding="utf-8") if out else None
    count = claim_total = 0
    try:
        for record in reader.iter_records():
            count += 1
            claim_total += len(record.claims)
            if sink is not None:
                sink.write(
                    json.dumps(
                        {
                            "source_record_id": record.source_record_id,
                            "idempotency_key": record.idempotency_key,
                            "captured_at": record.captured_at.isoformat(),
                            "claims": [c.model_dump(mode="json") for c in record.claims],
                            "raw_payload": record.raw_payload,
                        }
                    )
                    + "\n"
                )
            if limit and count >= limit:
                break
    finally:
        if sink is not None:
            sink.close()
    avg = claim_total / count if count else 0
    typer.echo(
        f"Mapped {count} records ({avg:.1f} claims/record){' -> ' + str(out) if out else ''}"
    )


@app.command("load")
def load(
    csv: Path = typer.Option(..., help="Directory of exported eADMS CSVs"),
    limit: int = typer.Option(0, help="Stop after N records (0 = all)"),
    progress_every: int = typer.Option(500, help="Log progress every N records"),
) -> None:
    """Ingest NTSB records into Atlas through the IngestSourceData use case.

    Ensures a single NTSB ``Source`` (tier 1, EXTERNAL) exists, then submits one
    accident per ingestion run.  Each record commits in its own unit of work so
    a mid-run failure neither rolls back prior progress nor blocks resumption.
    """
    # Deferred imports: keep export/dry-run free of any DB dependency.
    from atlas.application.use_cases.ingest_source_data import IngestSourceData
    from atlas.domain.entities import Source
    from atlas.domain.enums import SourceKind
    from atlas.infrastructure.db.unit_of_work import create_uow
    from atlas.logging_config import setup_logging

    setup_logging()
    reader = NtsbEadmsReader(csv)

    async def run() -> None:
        # Resolve-or-create the NTSB source once, in its own transaction.
        async with create_uow() as uow:
            source = await uow.sources.get_by_name(NTSB_SOURCE_NAME)
            if source is None:
                source = Source(
                    name=NTSB_SOURCE_NAME,
                    kind=SourceKind.EXTERNAL,
                    reliability_tier=NTSB_RELIABILITY_TIER,
                    field_mapping_json=dict(NTSB_FIELD_MAPPING),
                )
                await uow.sources.add(source)
                typer.echo(
                    f"Registered source {NTSB_SOURCE_NAME!r} (id={source.id}, tier={source.reliability_tier})"
                )
            else:
                typer.echo(f"Using existing source {NTSB_SOURCE_NAME!r} (id={source.id})")
        source_id = source.id

        done = created = failed = 0
        for record in reader.iter_records():
            run_id = IngestSourceData.derive_ingestion_run_id(source_id, record.idempotency_key)
            try:
                async with create_uow() as uow:
                    result = await IngestSourceData(uow).execute_with_result(
                        source_id=source_id,
                        raw_payload=record.raw_payload,
                        ingestion_run_id=run_id,
                        claims_data=record.claims,
                        captured_at=record.captured_at,
                        source_record_id=record.source_record_id,
                    )
                done += 1
                created += int(result.event_created)
            except Exception as exc:
                failed += 1
                typer.echo(f"  ! {record.source_record_id}: {type(exc).__name__}: {exc}", err=True)
            if progress_every and done and done % progress_every == 0:
                typer.echo(f"  ... {done} ingested ({created} new events, {failed} failed)")
            if limit and done >= limit:
                break
        typer.echo(f"Done. ingested={done} new_events={created} failed={failed}")

    asyncio.run(run())


@app.command("warm-corpus")
def warm_corpus() -> None:
    """Verify and warm the Echo corpus cache in this process.

    This command loads all public projections through the same public DB path
    used by Echo workers.  Because the cache is in-process, a one-shot CLI run
    validates corpus availability but cannot warm an already-running API or
    worker container.  Use it after ``sync-corpus`` as an operational smoke
    test, or call the loader from the actual service process during startup.
    """
    from atlas.application.use_cases.echo_crossref import CachedCorpusLoader
    from atlas.infrastructure.db.unit_of_work import create_public_uow
    from atlas.logging_config import setup_logging

    setup_logging()
    CachedCorpusLoader.invalidate()

    async def run() -> None:
        async with create_public_uow() as uow:
            loader = CachedCorpusLoader(ttl_seconds=0)  # force a fresh load
            records = await loader.load(uow=uow)
            typer.echo(
                f"Corpus loaded: {len(records)} precedent records available "
                "through the public DB path."
            )

    asyncio.run(run())


@app.command("sync-corpus")
def sync_corpus(
    batch_size: int = typer.Option(500, help="Rows to upsert per batch"),
    progress_every: int = typer.Option(5000, help="Log progress every N rows"),
) -> None:
    """Sync public projections from the Atlas DB into the SMS DB.

    Implements the one-way public→private corpus sync for the split-topology
    deployment where ``PUBLIC_DATABASE_URL`` and ``DATABASE_URL`` point to
    different Postgres instances.

    Reads ``projected_accident_records`` from the public DB (via
    ``PUBLIC_DATABASE_URL``) and upserts them into the SMS DB (via
    ``DATABASE_URL``).  The target table has a foreign key to
    ``accident_events``, so this command also copies the required parent event
    rows first.  Safe to run repeatedly — upserts are idempotent on
    ``event_id``.  After completion, invalidates the Echo corpus cache so the
    next cross-reference run uses the refreshed data.

    In a single-database deployment (``PUBLIC_DATABASE_URL`` unset) this
    command is a no-op — both URLs resolve to the same DB, so the upsert
    writes rows to themselves.  The command will complete successfully and
    log a notice.
    """
    from sqlalchemy import select, update
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.ext.asyncio import AsyncSession

    from atlas.application.use_cases.echo_crossref import CachedCorpusLoader
    from atlas.config import get_settings
    from atlas.infrastructure.db.orm_models import (
        AccidentEventModel,
        ProjectedAccidentRecordModel,
    )
    from atlas.infrastructure.db.session import async_public_session_factory, async_session_factory
    from atlas.logging_config import setup_logging

    setup_logging()
    settings = get_settings()

    if not settings.public_database_url:
        typer.echo(
            "PUBLIC_DATABASE_URL is not set — running in single-database mode. "
            "Source and target are the same DB; sync is a no-op.",
            err=True,
        )
        return

    async def run() -> None:
        synced = 0
        next_progress = progress_every if progress_every > 0 else 0
        async with async_public_session_factory() as pub_session:
            async with async_session_factory() as sms_session:
                # Stream from public DB, batch-upsert into SMS DB.
                stmt = (
                    select(ProjectedAccidentRecordModel)
                    .order_by(ProjectedAccidentRecordModel.event_id)
                    .execution_options(yield_per=batch_size)
                )
                batch: list[dict] = []
                result = await pub_session.stream(stmt)
                async for projection in result.scalars():
                    batch.append(
                        {
                            "event_id": projection.event_id,
                            "projection_version": projection.projection_version,
                            "fields": projection.fields,
                            "completeness_score": projection.completeness_score,
                            "unresolved_conflict_fields": projection.unresolved_conflict_fields,
                            "updated_at": projection.updated_at,
                        }
                    )
                    if len(batch) >= batch_size:
                        await _upsert_batch(pub_session, sms_session, batch)
                        synced += len(batch)
                        batch.clear()
                        while next_progress and synced >= next_progress:
                            typer.echo(f"  ... {synced} rows synced")
                            next_progress += progress_every
                if batch:
                    await _upsert_batch(pub_session, sms_session, batch)
                    synced += len(batch)
                await sms_session.commit()

        CachedCorpusLoader.invalidate()
        typer.echo(f"Sync complete: {synced} projection rows upserted into SMS DB.")

    async def _load_event_closure(
        pub_session: AsyncSession,
        event_ids: set,
    ) -> dict:
        """Load parent accident_events rows, following merged-event references.

        ``projected_accident_records.event_id`` has a foreign key to
        ``accident_events.id``.  Merged/tombstone projections may reference a
        survivor event via ``merged_into_event_id``; follow that chain too so
        self-referential FK updates can succeed in the target database.
        """
        pending = set(event_ids)
        loaded: dict = {}
        while pending:
            rows = await pub_session.scalars(
                select(AccidentEventModel).where(AccidentEventModel.id.in_(pending))
            )
            fetched = list(rows)
            missing = pending - {row.id for row in fetched}
            if missing:
                raise RuntimeError(
                    "Public projection rows reference missing accident_events: "
                    + ", ".join(str(i) for i in sorted(missing))
                )
            pending = set()
            for row in fetched:
                if row.id in loaded:
                    continue
                loaded[row.id] = row
                if row.merged_into_event_id and row.merged_into_event_id not in loaded:
                    pending.add(row.merged_into_event_id)
        return loaded

    async def _upsert_batch(
        pub_session: AsyncSession,
        sms_session: AsyncSession,
        rows: list[dict],
    ) -> None:
        if not rows:
            return

        # 1. Ensure parent accident_events exist before inserting projections.
        event_rows = await _load_event_closure(
            pub_session,
            {row["event_id"] for row in rows},
        )
        parent_values = [
            {
                "id": row.id,
                "created_at": row.created_at,
                # Insert with NULL first so self-referential merge chains do
                # not depend on insert ordering.  We restore the merge pointers
                # after all parent rows exist in the target DB.
                "merged_into_event_id": None,
            }
            for row in event_rows.values()
        ]
        parent_stmt = pg_insert(AccidentEventModel).values(parent_values)
        parent_stmt = parent_stmt.on_conflict_do_nothing(index_elements=["id"])
        await sms_session.execute(parent_stmt)

        for row in event_rows.values():
            await sms_session.execute(
                update(AccidentEventModel)
                .where(AccidentEventModel.id == row.id)
                .values(merged_into_event_id=row.merged_into_event_id)
            )

        # 2. Upsert the actual corpus projection rows.
        stmt = pg_insert(ProjectedAccidentRecordModel).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["event_id"],
            set_={
                "projection_version": stmt.excluded.projection_version,
                "fields": stmt.excluded.fields,
                "completeness_score": stmt.excluded.completeness_score,
                "unresolved_conflict_fields": stmt.excluded.unresolved_conflict_fields,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        await sms_session.execute(stmt)

    asyncio.run(run())


if __name__ == "__main__":
    app()
