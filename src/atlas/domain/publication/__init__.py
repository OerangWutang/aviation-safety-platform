"""Publication bounded context.

This package owns the editorial overlay that sits above the evidence chain
and projection model.  It is intentionally separate from ``atlas.domain``
core entities so that public pages cannot be confused with projected truth,
and the publication tables are easy to reason about as a thin overlay.

What lives here:

- :class:`PublicationStatus`: the full editorial lifecycle.
- :class:`PublicEventPage`: a publication-metadata domain entity.
- :class:`PublicEventPageRevision`: immutable audit rows.
- :mod:`workflow`: the editorial state machine.
- :func:`normalize_slug`: pure slug normaliser.
"""
