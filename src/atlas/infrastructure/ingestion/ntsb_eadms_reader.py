"""I/O layer for the NTSB eADMS importer.

Responsibilities (all the *impure* parts the mapping core deliberately avoids):

1. Optionally export the needed tables out of ``avall.mdb`` to CSV.  This is the
   **only** code that touches Microsoft Access; production deployments can run
   the export step once (or use NTSB's published CSVs) and never depend on
   ``mdbtools`` at request time.
2. Read those CSVs with a real CSV parser - ``narratives`` memo fields contain
   embedded newlines and commas, so naive line splitting is wrong.
3. Build the ``EadmsCodeDecoder`` from the in-DB data dictionary.
4. Stream joined ``NtsbEventRecord`` objects (one per accident), grouping the
   child tables by ``ev_id``.

The child tables (aircraft/narratives/findings) are indexed in memory keyed by
``ev_id``; events are then streamed one at a time.  On the public NTSB dataset
(~30k events, ~175MB of CSV) this peaks at a few hundred MB - acceptable for a
batch importer and far simpler than a multi-pass merge join.
"""

from __future__ import annotations

import csv
import subprocess
import sys
from collections import defaultdict
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from atlas.application.ingestion.sources.ntsb_eadms import (
    EadmsCodeDecoder,
    NtsbEventRecord,
    build_event_record,
)

# Tables the importer needs, in the order the export step writes them.
EADMS_TABLES = ("events", "aircraft", "narratives", "Findings", "eADMSPUB_DataDictionary")

# Python's CSV parser caps field size; eADMS probable-cause memos can be long.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def export_mdb_tables(
    mdb_path: Path, out_dir: Path, *, tables: tuple[str, ...] = EADMS_TABLES
) -> dict[str, Path]:
    """Export the needed tables from ``avall.mdb`` to ``out_dir/<table>.csv``.

    Shells to ``mdb-export`` (mdbtools).  Raises ``FileNotFoundError`` if the
    tool is absent, with a clear hint - we do not silently produce empty output.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for table in tables:
        dest = out_dir / f"{table}.csv"
        try:
            with dest.open("w", encoding="utf-8") as fh:
                subprocess.run(
                    ["mdb-export", str(mdb_path), table],
                    check=True,
                    stdout=fh,
                )
        except FileNotFoundError as exc:  # pragma: no cover - environment dependent
            raise FileNotFoundError(
                "mdb-export not found. Install mdbtools (apt-get install mdbtools) "
                "or supply pre-exported CSVs and skip the export step."
            ) from exc
        written[table] = dest
    return written


def _read_csv(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        yield from csv.DictReader(fh)


def load_decoder(csv_dir: Path) -> EadmsCodeDecoder:
    """Build the code decoder from the exported data-dictionary CSV."""
    return EadmsCodeDecoder.from_dictionary_rows(_read_csv(csv_dir / "eADMSPUB_DataDictionary.csv"))


def _index_by_ev_id(path: Path) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _read_csv(path):
        ev_id = (row.get("ev_id") or "").strip()
        if ev_id:
            index[ev_id].append(row)
    return index


class NtsbEadmsReader:
    """Stream :class:`NtsbEventRecord` objects from exported eADMS CSVs."""

    def __init__(self, csv_dir: Path) -> None:
        self._dir = Path(csv_dir)
        missing = [t for t in EADMS_TABLES if not (self._dir / f"{t}.csv").exists()]
        if missing:
            raise FileNotFoundError(
                f"missing exported CSVs in {self._dir}: {', '.join(missing)}. "
                "Run the export step first."
            )

    def iter_records(self, *, captured_at: datetime | None = None) -> Iterator[NtsbEventRecord]:
        decoder = load_decoder(self._dir)
        aircraft_idx = _index_by_ev_id(self._dir / "aircraft.csv")
        narrative_idx = _index_by_ev_id(self._dir / "narratives.csv")
        finding_idx = self._findings_index()

        for event_row in _read_csv(self._dir / "events.csv"):
            ev_id = (event_row.get("ev_id") or "").strip()
            if not ev_id:
                continue
            record = build_event_record(
                event_row=event_row,
                aircraft_rows=aircraft_idx.get(ev_id, []),  # type: ignore[arg-type]  # dict IS Mapping
                narrative_rows=narrative_idx.get(ev_id, []),  # type: ignore[arg-type]  # dict IS Mapping
                finding_rows=finding_idx.get(ev_id, []),  # type: ignore[arg-type]  # dict IS Mapping
                decoder=decoder,
                captured_at=captured_at,
            )
            if record is not None:
                yield record

    def _findings_index(self) -> dict[str, list[dict[str, Any]]]:
        # Preserve finding_no order within an event so the structured claim is
        # deterministic across runs.
        index: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in _read_csv(self._dir / "Findings.csv"):
            ev_id = (row.get("ev_id") or "").strip()
            if ev_id:
                index[ev_id].append(row)
        for rows in index.values():
            rows.sort(key=lambda r: _safe_int(r.get("finding_no")))
        return index


def _safe_int(value: Any) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0
