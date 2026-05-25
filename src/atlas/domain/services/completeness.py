from atlas.domain.constants import DISPUTED, DISPUTED_MARKER
from atlas.domain.enums import RequiredField


class CompletenessCalculator:
    DEFAULT_REQUIRED_FIELDS: frozenset[str] = frozenset(field.value for field in RequiredField)

    def __init__(self, required_fields: frozenset[str] | None = None) -> None:
        self.required_fields = (
            required_fields if required_fields is not None else self.DEFAULT_REQUIRED_FIELDS
        )

    def score(self, fields: dict[str, object]) -> float:
        """Return fraction of required fields present and not disputed.

        A field counts toward the score only when it is present, non-None,
        and not the DISPUTED sentinel. Open conflicts therefore reduce
        completeness just like missing data.
        """
        if not self.required_fields:
            return 1.0
        present = sum(
            1
            for field in self.required_fields
            if (value := fields.get(field)) is not None
            and value is not DISPUTED
            and value != DISPUTED_MARKER
        )
        return float(present / len(self.required_fields))
