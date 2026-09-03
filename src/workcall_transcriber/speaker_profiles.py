"""Conservative local speaker-profile matching with cosine similarity."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SpeakerProfile:
    id: str
    display_name: str
    centroid: tuple[float, ...]
    reference_count: int


@dataclass(frozen=True)
class ProfileMatch:
    profile_id: str
    display_name: str
    similarity: float
    threshold: float
    method: str = "cosine_similarity"


def match_embedding(
    embedding: Sequence[float],
    profiles: Iterable[SpeakerProfile],
    *,
    threshold: float,
    ambiguity_margin: float = 0.02,
) -> ProfileMatch | None:
    """Return a match only when it is strong and clearly better than alternatives.

    The threshold is an explicit user setting, not a claim of a universal voice
    identity cutoff. A near tie is intentionally left as ``SPEAKER_XX``.
    """
    if not 0.0 < threshold <= 1.0:
        raise ValueError("Speaker matching threshold must be between 0 and 1.")
    candidates = sorted(
        (
            (cosine_similarity(embedding, profile.centroid), profile)
            for profile in profiles
            if len(profile.centroid) == len(embedding)
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )
    if not candidates:
        return None
    similarity, profile = candidates[0]
    if similarity < threshold:
        return None
    if len(candidates) > 1 and similarity - candidates[1][0] < ambiguity_margin:
        return None
    return ProfileMatch(profile.id, profile.display_name, similarity, threshold)


def updated_centroid(
    current: Sequence[float],
    reference_count: int,
    new_embedding: Sequence[float],
) -> tuple[float, ...]:
    """Fold one opted-in reference into a normalized profile centroid."""
    if len(current) != len(new_embedding) or not current:
        raise ValueError("Speaker embeddings must have equal non-zero dimensions.")
    if reference_count < 1:
        return normalize(new_embedding)
    weighted = [
        (float(existing) * reference_count + float(incoming)) / (reference_count + 1)
        for existing, incoming in zip(current, new_embedding, strict=True)
    ]
    return normalize(weighted)


def apply_profile_matches(
    result: dict[str, Any],
    embeddings: dict[str, Sequence[float]] | None,
    profiles: Iterable[SpeakerProfile],
    *,
    threshold: float,
) -> dict[str, ProfileMatch]:
    """Annotate WhisperX segments while preserving every raw diarization label."""
    matches: dict[str, ProfileMatch] = {}
    for raw_label, embedding in (embeddings or {}).items():
        match = match_embedding(embedding, profiles, threshold=threshold)
        if match is not None:
            matches[raw_label] = match
    for segment in result.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        match = matches.get(segment.get("speaker"))
        if match is None:
            continue
        segment["speaker_identity"] = match.display_name
        segment["speaker_identity_confidence"] = match.similarity
        segment["speaker_match_threshold"] = match.threshold
        segment["speaker_matching_method"] = match.method
    return matches


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("Speaker embeddings must have equal non-zero dimensions.")
    numerator = sum(float(a) * float(b) for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    if left_norm == 0 or right_norm == 0:
        return -1.0
    return numerator / (left_norm * right_norm)


def normalize(vector: Sequence[float]) -> tuple[float, ...]:
    magnitude = math.sqrt(sum(float(value) ** 2 for value in vector))
    if magnitude == 0:
        raise ValueError("Speaker embedding cannot be all zeros.")
    return tuple(float(value) / magnitude for value in vector)
