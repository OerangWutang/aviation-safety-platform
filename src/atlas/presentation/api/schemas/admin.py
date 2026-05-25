from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class RebuildRequest(BaseModel):
    event_id: UUID | None = None
    all: bool = False
    batch_size: int = Field(default=100, ge=1, le=500)
    max_events: int | None = Field(
        default=None,
        description=(
            "Required when all=true. Pass -1 for no cap (use with caution). "
            "Any other value must be >= 1."
        ),
    )

    @model_validator(mode="after")
    def validate_rebuild_request(self) -> "RebuildRequest":
        if self.all and self.event_id is not None:
            raise ValueError("Use either all=true or event_id, not both")

        if self.all:
            if self.max_events is None:
                raise ValueError(
                    "max_events is required when all=true. "
                    "Pass max_events=-1 to explicitly rebuild all events with no cap."
                )
            if self.max_events != -1 and self.max_events < 1:
                raise ValueError("max_events must be -1 (unlimited) or a positive integer >= 1")

        if not self.all and self.event_id is None:
            raise ValueError(
                "Provide event_id to rebuild a single event, or set all=true to rebuild all."
            )

        return self


class RebuildResponse(BaseModel):
    processed: int
    skipped: int = 0
    failed_event_ids: list[UUID] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    message: str


class MergeRequest(BaseModel):
    source_event_id: UUID = Field(
        description="The duplicate event to be absorbed (will be marked merged)."
    )
    target_event_id: UUID = Field(
        description="The surviving event that receives the merged claims."
    )
    note: str = Field(
        default="",
        max_length=500,
        description="Optional curator note recorded on all affected ClaimHistory rows.",
    )


class MergeResponse(BaseModel):
    target_event_id: UUID
    source_event_id: UUID
    claims_moved: int
    message: str


class ReviewActionRequest(BaseModel):
    action: Literal["confirm", "reject"] = Field(
        description=(
            "'confirm' triggers a merge of the two events. "
            "'reject' marks the pair as distinct accidents."
        )
    )
    source_event_id: UUID | None = Field(
        default=None,
        description=(
            "Which of the two events to absorb when confirming. "
            "Defaults to event_id_b (the newer event)."
        ),
    )
    note: str = Field(default="", max_length=500)


class ReviewActionResponse(BaseModel):
    review_id: UUID
    action: str
    message: str
    # merge_result is populated when action='confirm' and the merge completed.
    # It is None for action='reject'.
    merge_result: MergeResponse | None = None


class SetSourceFieldMappingRequest(BaseModel):
    """Replace the entire ``field_mapping_json`` for a source.

    Keys are raw source field names (e.g. ``"date"``, ``"tailNumber"``,
    ``"Aircraft Registration"``); values are canonical Atlas field names
    drawn from ``RequiredField`` (``"event_date"``, ``"registration"``,
    ``"operator"``, etc.).  Raw keys are matched tolerantly
    (snake_case/camelCase/spaces/hyphens) but two raw keys that normalise to
    the same form are rejected so the resulting mapping is unambiguous.
    """

    field_mapping: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Raw-field-name -> canonical-Atlas-field mapping.  Pass an empty "
            "object to clear the mapping for this source.  Unknown canonical "
            "targets are rejected with 422 before any write."
        ),
    )


class SetSourceFieldMappingResponse(BaseModel):
    source_id: UUID
    field_mapping: dict[str, str]
    entry_count: int
