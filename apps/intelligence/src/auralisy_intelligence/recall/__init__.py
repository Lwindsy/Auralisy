"""Album recall port for the intelligence application."""

from dataclasses import replace
from pathlib import Path

import numpy as np

from .c_v0 import load_comparison
from .common import recall as _rank
from .common import validate_request
from .genre_reach import ReachFilter
from .io import load_dataset
from .neighborhood import NeighborhoodConfig, NeighborhoodModel
from .types import Candidate, Preference, RecallModel, RecallRequest, RecallResult, WeightConfig

__all__ = [
    "Candidate",
    "Preference",
    "RecallEngine",
    "RecallRequest",
    "RecallResult",
    "WeightConfig",
    "load_a_v2",
    "load_c_v0",
]


class RecallEngine:
    """A reusable scorer bound to a snapshot and caller-provided preference weights."""

    def __init__(self, model: RecallModel, algorithm: str):
        self._model = model
        self._algorithm = algorithm
        self._full_audience = None

    def bind_full_audience(self, audience) -> None:
        """Install an independently admitted population; never mutate training statistics."""
        self._full_audience = audience

    def bind_reach_index(self, index) -> None:
        self._reach_filter = ReachFilter(index, self._model.data, self._full_audience)

    @property
    def audience_binding(self) -> dict | None:
        return self._full_audience.binding_copy() if self._full_audience is not None else None

    def recall(
        self,
        request: RecallRequest,
        *,
        min_listeners: int | None = None,
        max_listeners: int | None = None,
        reach_percent: int | None = None,
        reference_request: RecallRequest | None = None,
    ) -> RecallResult:
        mask, fallback = None, None
        if reach_percent is not None:
            validate_request(self._model.data, request, self._model.weights)
            if self._full_audience is None:
                raise ValueError("FULL_AUDIENCE_REQUIRED")
            if not hasattr(self, "_reach_filter"):
                raise ValueError("REACH_INDEX_REQUIRED")
            if type(reach_percent) is not int or not 0 <= reach_percent <= 100:
                raise ValueError("REACH_POSITION_INVALID")
            mask, fallback = self._reach_filter.evaluate(
                reference_request or request, reach_percent, self._model.weights
            )
        elif min_listeners is not None or max_listeners is not None:
            minimum = min_listeners if min_listeners is not None else 0
            maximum = max_listeners if max_listeners is not None else float("inf")
            if not 0 <= minimum <= maximum:
                raise ValueError("invalid audience range")
            if self._full_audience is None:
                raise ValueError("FULL_AUDIENCE_REQUIRED")
            counts = self._audience_counts()
            limit = np.iinfo(np.int64).max
            if minimum > limit:
                mask = np.zeros(len(counts), dtype=bool)
            else:
                upper = np.inf if maximum > limit else maximum
                mask = (counts >= minimum) & (counts <= upper)
        return replace(
            _rank(self._model, request, eligibility_mask=mask, fallback_scores=fallback),
            algorithm=self._algorithm,
        )

    def _audience_counts(self) -> np.ndarray:
        """Full-audience listener counts aligned with album IDs; -1 marks an absent album.

        Built once per bound audience and ID sequence. Testing each of ~828K albums against a
        dict on every request was a dominant, GIL-holding cost under concurrent rounds.
        """
        audience, identifiers = self._full_audience, self._model.data.album_ids
        cached = getattr(self, "_audience_count_cache", None)
        if (
            cached is None
            or cached[0] is not audience
            or cached[1] is not identifiers
            or type(identifiers) is not tuple
        ):
            counts = audience.counts
            values = np.fromiter(
                (counts.get(identifier, -1) for identifier in identifiers),
                dtype=np.int64,
                count=len(identifiers),
            )
            cached = (audience, identifiers, values)
            self._audience_count_cache = cached
        return cached[2]

    def audience(self, album_ids: list[str]) -> dict:
        """Product admission requires the independently bound full population."""
        if self._full_audience is None:
            raise ValueError("FULL_AUDIENCE_REQUIRED")
        return self._full_audience.audience(album_ids)

    def training_audience(self, album_ids: list[str]) -> dict:
        """Research-only statistics; never used for product eligibility."""
        if not hasattr(self, "_audience"):
            data = self._model.data
            counts = data.interactions.getnnz(axis=0)
            self._audience = dict(zip(data.album_ids, map(int, counts)))
        return {
            "dataset_id": self._model.data.dataset_id,
            "window": self._model.data.window,
            "sample_listeners": self._model.data.interactions.shape[0],
            "counts": {identifier: self._audience.get(identifier) for identifier in album_ids},
        }


def load_a_v2(snapshot: Path, *, weights: WeightConfig) -> RecallEngine:
    """Load a caller-authorized snapshot and build the A-V2 CPU statistics."""
    data = load_dataset(Path(snapshot))
    model = NeighborhoodModel(NeighborhoodConfig(tag_fraction=0.0), weights).fit(data)
    return RecallEngine(model, "A-V2")


def load_c_v0(snapshot: Path, artifact: Path, *, weights: WeightConfig) -> RecallEngine:
    """Load the comparison baseline using a caller-authorized local artifact."""
    return RecallEngine(load_comparison(Path(snapshot), Path(artifact), weights), "C-V0")
