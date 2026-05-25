"""Use-case tests for the Phase 3 map surface.

Three concerns covered here:

1. **Query validation** — :class:`MapBoundingBox` and
   :class:`MapQuery` reject malformed inputs at construction.

2. **Lifecycle invariant** — the map index contains exactly the set
   of PUBLISHED pages that have parseable coordinates.  Driven
   through the Phase 9 publish / archive / retract paths so the
   integration between the phases is exercised end to end.

3. **Read paths** — :class:`SearchMapPoints` and
   :class:`ClusterMapPoints` compose the repository correctly:
   bbox filter, antimeridian crossing, additional-facet filters,
   cluster grid math.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from atlas.application.use_cases.editorial import (
    ApprovePublicEventPage,
    ArchivePublicEventPage,
    CreatePublicEventPage,
    CreatePublicEventPageInput,
    PublishPublicEventPage,
    RetractPublicEventPage,
    SubmitPublicEventPage,
    TransitionPublicEventPageInput,
)
from atlas.application.use_cases.map_events import (
    ClusterMapPoints,
    SearchMapPoints,
)
from atlas.application.use_cases.reindex_public_events import ReindexPublicEvents
from atlas.domain.entities import AccidentEvent, ProjectedAccidentRecord
from atlas.domain.maps.entities import (
    MAX_POINTS_PER_RESPONSE,
    MapBoundingBox,
    MapQuery,
)
from atlas.domain.maps.exceptions import MapQueryMalformedError
from tests.domain._fake_uow import InMemoryUnitOfWork

# ── Helpers ──────────────────────────────────────────────────────────────────


def _seed_event_with_coords(
    uow: InMemoryUnitOfWork,
    *,
    lat: float,
    lng: float,
    operator: str = "ABC Airlines",
    aircraft_type: str = "Boeing 737-800",
    country: str = "United States",
    event_date: str = "2024-06-01",
    fatalities_total: int = 0,
):
    event = AccidentEvent()
    uow.store.events[event.id] = event
    uow.store.projections[event.id] = ProjectedAccidentRecord(
        event_id=event.id,
        fields={
            "operator": operator,
            "aircraft_type": aircraft_type,
            "country": country,
            "event_date": event_date,
            "fatalities_total": fatalities_total,
            "latitude": lat,
            "longitude": lng,
        },
        completeness_score=0.9,
    )
    return event.id


def _seed_event_without_coords(uow: InMemoryUnitOfWork):
    event = AccidentEvent()
    uow.store.events[event.id] = event
    uow.store.projections[event.id] = ProjectedAccidentRecord(
        event_id=event.id,
        fields={"operator": "X"},
        completeness_score=0.5,
    )
    return event.id


async def _publish(uow: InMemoryUnitOfWork, *, event_id, slug: str, title: str):
    """Drive a fresh event through Create → Submit → Approve → Publish."""
    user = uuid4()
    page = await CreatePublicEventPage(uow).execute(
        CreatePublicEventPageInput(event_id=event_id, slug=slug, title=title, editor_user_id=user)
    )
    page = await SubmitPublicEventPage(uow).execute(
        TransitionPublicEventPageInput(
            page_id=page.id,
            expected_version=page.version,
            editor_user_id=user,
        )
    )
    page = await ApprovePublicEventPage(uow).execute(
        TransitionPublicEventPageInput(
            page_id=page.id,
            expected_version=page.version,
            editor_user_id=user,
        )
    )
    page = await PublishPublicEventPage(uow).execute(
        TransitionPublicEventPageInput(
            page_id=page.id,
            expected_version=page.version,
            editor_user_id=user,
        )
    )
    return page


# ── Query validation ────────────────────────────────────────────────────────


class TestMapQueryValidation:
    def test_out_of_range_latitude_rejected(self) -> None:
        with pytest.raises(MapQueryMalformedError):
            MapBoundingBox(south=-95.0, west=-10.0, north=10.0, east=10.0)

    def test_out_of_range_longitude_rejected(self) -> None:
        with pytest.raises(MapQueryMalformedError):
            MapBoundingBox(south=0.0, west=-200.0, north=10.0, east=10.0)

    def test_inverted_lat_range_rejected(self) -> None:
        """south > north is always wrong; lng can wrap but lat cannot."""
        with pytest.raises(MapQueryMalformedError):
            MapBoundingBox(south=10.0, west=0.0, north=5.0, east=10.0)

    def test_antimeridian_crossing_box_is_valid(self) -> None:
        """West > east is the antimeridian-crossing case, not an error."""
        bbox = MapBoundingBox(south=-10.0, west=170.0, north=10.0, east=-170.0)
        assert bbox.crosses_antimeridian is True
        # Longitude span includes the wrap: 10 east of dateline + 10 west = 20°.
        assert bbox.longitude_span == pytest.approx(20.0)

    def test_inverted_fatalities_range_rejected(self) -> None:
        bbox = MapBoundingBox(south=-1, west=-1, north=1, east=1)
        with pytest.raises(MapQueryMalformedError):
            MapQuery(bbox=bbox, fatalities_min=10, fatalities_max=5)

    def test_inverted_date_range_rejected(self) -> None:
        from datetime import date

        bbox = MapBoundingBox(south=-1, west=-1, north=1, east=1)
        with pytest.raises(MapQueryMalformedError):
            MapQuery(
                bbox=bbox,
                event_date_from=date(2024, 6, 1),
                event_date_to=date(2024, 1, 1),
            )

    def test_oversized_limit_rejected(self) -> None:
        bbox = MapBoundingBox(south=-1, west=-1, north=1, east=1)
        with pytest.raises(MapQueryMalformedError):
            MapQuery(bbox=bbox, limit=MAX_POINTS_PER_RESPONSE + 1)

    def test_cluster_precision_bounds_enforced(self) -> None:
        bbox = MapBoundingBox(south=-1, west=-1, north=1, east=1)
        with pytest.raises(MapQueryMalformedError):
            MapQuery(bbox=bbox, cluster=True, cluster_precision=1)
        with pytest.raises(MapQueryMalformedError):
            MapQuery(bbox=bbox, cluster=True, cluster_precision=999)


# ── Lifecycle invariant ─────────────────────────────────────────────────────


class TestMapIndexLifecycle:
    """Map index == PUBLISHED pages that have parseable coordinates."""

    async def test_publish_inserts_into_map(self) -> None:
        uow = InMemoryUnitOfWork()
        event_id = _seed_event_with_coords(uow, lat=37.0, lng=-122.0)
        page = await _publish(uow, event_id=event_id, slug="x", title="X")
        assert page.id in uow.store.maps.entries
        entry = uow.store.maps.entries[page.id]
        assert entry.latitude == pytest.approx(37.0)
        assert entry.longitude == pytest.approx(-122.0)

    async def test_event_without_coords_not_indexed(self) -> None:
        """A published page whose projection has no parseable lat/lng
        is *not* added to the map index.  The publish still
        succeeds — Phase 2 search still gets it."""
        uow = InMemoryUnitOfWork()
        event_id = _seed_event_without_coords(uow)
        page = await _publish(uow, event_id=event_id, slug="no-loc", title="No loc")
        assert page.id not in uow.store.maps.entries
        # Phase 2 search still indexed it though — the search and map
        # indices are independent projections of "PUBLISHED + ...".
        assert page.id in uow.store.search.entries

    async def test_archive_removes_from_map(self) -> None:
        uow = InMemoryUnitOfWork()
        event_id = _seed_event_with_coords(uow, lat=10.0, lng=10.0)
        page = await _publish(uow, event_id=event_id, slug="rm", title="X")
        assert page.id in uow.store.maps.entries
        page = await ArchivePublicEventPage(uow).execute(
            TransitionPublicEventPageInput(
                page_id=page.id,
                expected_version=page.version,
                editor_user_id=uuid4(),
            )
        )
        assert page.id not in uow.store.maps.entries

    async def test_retract_removes_from_map(self) -> None:
        uow = InMemoryUnitOfWork()
        event_id = _seed_event_with_coords(uow, lat=10.0, lng=10.0)
        page = await _publish(uow, event_id=event_id, slug="ret", title="X")
        page = await RetractPublicEventPage(uow).execute(
            TransitionPublicEventPageInput(
                page_id=page.id,
                expected_version=page.version,
                editor_user_id=uuid4(),
                retraction_note="x",
            )
        )
        assert page.id not in uow.store.maps.entries

    async def test_coords_appearing_on_republish_indexes_the_page(self) -> None:
        """A page that was published without coords gets indexed on
        re-publish if coordinates have since been added to the
        projection.  Mirrors the defensive-delete-or-upsert path in
        ``index_published_page_in_map``."""
        uow = InMemoryUnitOfWork()
        event_id = _seed_event_without_coords(uow)
        page = await _publish(uow, event_id=event_id, slug="late", title="X")
        assert page.id not in uow.store.maps.entries

        # An editor (or a re-projection) now fills coordinates.
        uow.store.projections[event_id].fields["latitude"] = 1.0
        uow.store.projections[event_id].fields["longitude"] = 2.0

        # Archive then republish — re-publishing from archive re-runs
        # the post-transition hook.
        page = await ArchivePublicEventPage(uow).execute(
            TransitionPublicEventPageInput(
                page_id=page.id,
                expected_version=page.version,
                editor_user_id=uuid4(),
            )
        )
        page = await PublishPublicEventPage(uow).execute(
            TransitionPublicEventPageInput(
                page_id=page.id,
                expected_version=page.version,
                editor_user_id=uuid4(),
            )
        )
        assert page.id in uow.store.maps.entries

    async def test_coords_disappearing_unindexes_the_page(self) -> None:
        """The mirror case: a page indexed with coords, but a later
        re-projection produces a projection without coords, gets
        removed from the index on re-publish."""
        uow = InMemoryUnitOfWork()
        event_id = _seed_event_with_coords(uow, lat=5.0, lng=5.0)
        page = await _publish(uow, event_id=event_id, slug="cyc", title="X")
        assert page.id in uow.store.maps.entries

        # Remove coords from the projection.
        del uow.store.projections[event_id].fields["latitude"]
        del uow.store.projections[event_id].fields["longitude"]

        page = await ArchivePublicEventPage(uow).execute(
            TransitionPublicEventPageInput(
                page_id=page.id,
                expected_version=page.version,
                editor_user_id=uuid4(),
            )
        )
        page = await PublishPublicEventPage(uow).execute(
            TransitionPublicEventPageInput(
                page_id=page.id,
                expected_version=page.version,
                editor_user_id=uuid4(),
            )
        )
        # Defensive delete inside the upsert path removes the row.
        assert page.id not in uow.store.maps.entries

    async def test_invalid_coords_are_treated_as_missing(self) -> None:
        """Out-of-range coords don't blow up the publish; the page
        just isn't indexed in the map."""
        uow = InMemoryUnitOfWork()
        event = AccidentEvent()
        uow.store.events[event.id] = event
        uow.store.projections[event.id] = ProjectedAccidentRecord(
            event_id=event.id,
            fields={"latitude": 91.0, "longitude": 0.0},  # 91 > 90
            completeness_score=0.5,
        )
        page = await _publish(uow, event_id=event.id, slug="bad", title="X")
        assert page.id not in uow.store.maps.entries


# ── Read paths ──────────────────────────────────────────────────────────────


class TestSearchMapPoints:
    async def test_bbox_filters_in_and_out(self) -> None:
        uow = InMemoryUnitOfWork()
        await _publish(
            uow,
            event_id=_seed_event_with_coords(uow, lat=37.7, lng=-122.4),
            slug="sf",
            title="SF",
        )
        await _publish(
            uow,
            event_id=_seed_event_with_coords(uow, lat=40.7, lng=-74.0),
            slug="ny",
            title="NY",
        )
        # West-coast bbox: only SF should match.
        bbox = MapBoundingBox(south=30.0, west=-130.0, north=45.0, east=-115.0)
        result = await SearchMapPoints(uow).execute(MapQuery(bbox=bbox))
        slugs = {p.slug for p in result.items}
        assert slugs == {"sf"}

    async def test_antimeridian_crossing_bbox_finds_points_on_either_side(
        self,
    ) -> None:
        """A bbox spanning the dateline must find points on both
        sides of 180°/-180°.  This pins the antimeridian-aware
        predicate logic in both repos."""
        uow = InMemoryUnitOfWork()
        # Sydney-area point (positive lng), Wake-Island-area point
        # (negative lng), and a far-away point that must NOT match.
        await _publish(
            uow,
            event_id=_seed_event_with_coords(uow, lat=-33.0, lng=151.0),
            slug="aus",
            title="Australia",
        )
        await _publish(
            uow,
            event_id=_seed_event_with_coords(uow, lat=20.0, lng=-170.0),
            slug="wak",
            title="Wake-ish",
        )
        await _publish(
            uow,
            event_id=_seed_event_with_coords(uow, lat=0.0, lng=0.0),
            slug="atl",
            title="Atlantic",
        )
        # Box from 140°E through the dateline to -160° (so west=140,
        # east=-160).  Both Pacific points are inside; the Atlantic
        # one is not.
        bbox = MapBoundingBox(south=-50.0, west=140.0, north=40.0, east=-160.0)
        result = await SearchMapPoints(uow).execute(MapQuery(bbox=bbox))
        slugs = {p.slug for p in result.items}
        assert slugs == {"aus", "wak"}

    async def test_filters_compose_with_bbox(self) -> None:
        uow = InMemoryUnitOfWork()
        await _publish(
            uow,
            event_id=_seed_event_with_coords(uow, lat=10.0, lng=10.0, operator="ABC Airlines"),
            slug="abc",
            title="X",
        )
        await _publish(
            uow,
            event_id=_seed_event_with_coords(uow, lat=10.5, lng=10.5, operator="XYZ Airlines"),
            slug="xyz",
            title="Y",
        )
        bbox = MapBoundingBox(south=0.0, west=0.0, north=20.0, east=20.0)
        result = await SearchMapPoints(uow).execute(MapQuery(bbox=bbox, operator="ABC Airlines"))
        slugs = {p.slug for p in result.items}
        assert slugs == {"abc"}

    async def test_truncation_flag_set_when_over_limit(self) -> None:
        uow = InMemoryUnitOfWork()
        for i in range(5):
            await _publish(
                uow,
                event_id=_seed_event_with_coords(uow, lat=float(i), lng=float(i)),
                slug=f"p-{i}",
                title=f"P{i}",
            )
        bbox = MapBoundingBox(south=-1.0, west=-1.0, north=10.0, east=10.0)
        result = await SearchMapPoints(uow).execute(MapQuery(bbox=bbox, limit=2))
        assert len(result.items) == 2
        assert result.truncated is True


class TestClusterMapPoints:
    async def test_clusters_group_nearby_points(self) -> None:
        """Two SF-area points should cluster into one cell, and one
        NYC point into a different cell, when the bbox spans the
        continental US with a small grid."""
        uow = InMemoryUnitOfWork()
        await _publish(
            uow,
            event_id=_seed_event_with_coords(uow, lat=37.7, lng=-122.4),
            slug="sf1",
            title="SF1",
        )
        await _publish(
            uow,
            event_id=_seed_event_with_coords(uow, lat=37.8, lng=-122.5),
            slug="sf2",
            title="SF2",
        )
        await _publish(
            uow,
            event_id=_seed_event_with_coords(uow, lat=40.7, lng=-74.0),
            slug="ny",
            title="NY",
        )
        bbox = MapBoundingBox(south=24.0, west=-125.0, north=49.0, east=-66.0)
        result = await ClusterMapPoints(uow).execute(
            MapQuery(bbox=bbox, cluster=True, cluster_precision=8)
        )
        # Each cell carries a count; the SF cell must have count == 2.
        counts = sorted(c.count for c in result.cells)
        assert counts == [1, 2]

    async def test_cluster_centroid_pulls_toward_data(self) -> None:
        """Centroid is the average of the points' actual coordinates,
        not the cell's geometric centre — pins the AVG(lng), AVG(lat)
        contract."""
        uow = InMemoryUnitOfWork()
        # Two near-identical points; centroid should land very near
        # them, not at the cell centre.
        await _publish(
            uow,
            event_id=_seed_event_with_coords(uow, lat=37.7, lng=-122.4),
            slug="a",
            title="A",
        )
        await _publish(
            uow,
            event_id=_seed_event_with_coords(uow, lat=37.71, lng=-122.41),
            slug="b",
            title="B",
        )
        bbox = MapBoundingBox(south=20.0, west=-130.0, north=50.0, east=-100.0)
        result = await ClusterMapPoints(uow).execute(
            MapQuery(bbox=bbox, cluster=True, cluster_precision=4)
        )
        # Only one cluster (both points are very close).
        assert len(result.cells) == 1
        cell = result.cells[0]
        assert cell.centroid_latitude == pytest.approx(37.705, abs=0.01)
        assert cell.centroid_longitude == pytest.approx(-122.405, abs=0.01)
        assert cell.count == 2

    async def test_cluster_filters_compose(self) -> None:
        uow = InMemoryUnitOfWork()
        await _publish(
            uow,
            event_id=_seed_event_with_coords(uow, lat=10.0, lng=10.0, country="USA"),
            slug="usa",
            title="X",
        )
        await _publish(
            uow,
            event_id=_seed_event_with_coords(uow, lat=10.5, lng=10.5, country="Canada"),
            slug="can",
            title="Y",
        )
        bbox = MapBoundingBox(south=0.0, west=0.0, north=20.0, east=20.0)
        result = await ClusterMapPoints(uow).execute(
            MapQuery(
                bbox=bbox,
                cluster=True,
                cluster_precision=4,
                country="USA",
            )
        )
        assert len(result.cells) == 1
        assert result.cells[0].count == 1


# ── Admin reindex rebuilds both indices ──────────────────────────────────────


class TestReindexCoversMapIndex:
    async def test_reindex_rebuilds_map_index(self) -> None:
        """The admin reindex endpoint walks PUBLISHED pages and
        repopulates the map index alongside the search index.

        This covers the bootstrap case: when migration 039 ships,
        the map table is empty.  Operators run reindex; both
        indices come back into sync."""
        uow = InMemoryUnitOfWork()
        await _publish(
            uow,
            event_id=_seed_event_with_coords(uow, lat=10.0, lng=10.0),
            slug="a",
            title="A",
        )
        await _publish(
            uow,
            event_id=_seed_event_with_coords(uow, lat=20.0, lng=20.0),
            slug="b",
            title="B",
        )
        # A third page without coords — must NOT appear in the map
        # index but must appear in the search index.
        await _publish(
            uow,
            event_id=_seed_event_without_coords(uow),
            slug="c",
            title="C",
        )

        # Wipe both indices.
        uow.store.search.entries.clear()
        uow.store.maps.entries.clear()

        result = await ReindexPublicEvents(uow).execute()
        assert result.pages_reindexed == 3
        # Only the two pages with coords got into the map.
        assert result.map_pages_reindexed == 2
        assert len(uow.store.maps.entries) == 2
        assert len(uow.store.search.entries) == 3
