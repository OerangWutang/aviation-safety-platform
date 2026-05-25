from __future__ import annotations

from typing import Any, Final, Literal

DISPUTED_MARKER: Final[Literal["__DISPUTED__"]] = "__DISPUTED__"
MAX_REGISTRATION_ALIASES: Final[int] = 5


class DisputedType:
    """Singleton type for fields under active dispute.

    Use identity checks (`value is DISPUTED`) inside Python. When represented
    in JSON, the sentinel becomes the stable marker string `__DISPUTED__`.

    Pydantic v1 validation is provided via ``__get_validators__``.
    Pydantic v2 validation is provided via ``__get_pydantic_core_schema__``.
    Both produce the same ``DISPUTED`` singleton.
    """

    _instance: DisputedType | None = None

    def __new__(cls) -> DisputedType:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "DISPUTED"

    def __str__(self) -> str:
        return DISPUTED_MARKER

    # ── Pydantic v1 compatibility ─────────────────────────────────────────────
    @classmethod
    def __get_validators__(cls):
        yield cls._validate

    @classmethod
    def _validate(cls, value: object) -> DisputedType:
        if value is DISPUTED or value == DISPUTED_MARKER:
            return DISPUTED
        raise ValueError(f"Expected DISPUTED sentinel, got {value!r}")

    # ── Pydantic v2 compatibility ─────────────────────────────────────────────
    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any) -> Any:
        """Return a Pydantic v2 core schema that validates and serializes DISPUTED.

        Validation accepts either the ``DISPUTED`` singleton or the string
        ``"__DISPUTED__"``; both produce the singleton.  Serialization always
        produces the stable string form so JSON payloads are portable.
        """
        from pydantic_core import core_schema  # local import avoids hard dep at module level

        def validate(value: object) -> DisputedType:
            return cls._validate(value)

        return core_schema.no_info_plain_validator_function(
            validate,
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda _v: DISPUTED_MARKER,
                info_arg=False,
            ),
        )


DISPUTED: Final[DisputedType] = DisputedType()


def replace_disputed(value: Any) -> Any:
    """Recursively convert DISPUTED sentinels to their JSON marker string."""
    if value is DISPUTED or isinstance(value, DisputedType):
        return DISPUTED_MARKER
    if isinstance(value, dict):
        return {key: replace_disputed(item) for key, item in value.items()}
    if isinstance(value, list):
        return [replace_disputed(item) for item in value]
    return value
