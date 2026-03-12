from __future__ import annotations

from typing import List, Optional

from qdrant_client import models as qdrant_models

from automem.utils.tags import _prepare_tag_filters


def _build_qdrant_tag_filter(
    tags: Optional[List[str]],
    mode: str = "any",
    match: str = "exact",
):
    """Build a Qdrant filter for tag constraints, supporting mode/match semantics.

    Extracted for reuse by Qdrant interactions.
    """
    normalized_tags = _prepare_tag_filters(tags)
    if not normalized_tags:
        return None

    target_key = "tag_prefixes" if match == "prefix" else "tags"
    normalized_mode = "all" if mode == "all" else "any"

    if normalized_mode == "any":
        return qdrant_models.Filter(
            must=[
                qdrant_models.FieldCondition(
                    key=target_key,
                    match=qdrant_models.MatchAny(any=normalized_tags),
                )
            ]
        )

    must_conditions = [
        qdrant_models.FieldCondition(
            key=target_key,
            match=qdrant_models.MatchValue(value=tag),
        )
        for tag in normalized_tags
    ]

    return qdrant_models.Filter(must=must_conditions)


def _build_qdrant_bank_condition(bank: str):
    """Build a Qdrant field condition for bank filtering."""
    return qdrant_models.FieldCondition(
        key="bank",
        match=qdrant_models.MatchValue(value=bank),
    )


def _merge_qdrant_filters(
    tag_filter: Optional[qdrant_models.Filter],
    bank: Optional[str] = None,
) -> Optional[qdrant_models.Filter]:
    """Merge tag filter with bank filter into a single Qdrant Filter."""
    conditions = []
    if tag_filter and tag_filter.must:
        conditions.extend(tag_filter.must)
    if bank and bank != "default":
        conditions.append(_build_qdrant_bank_condition(bank))
    if not conditions:
        return tag_filter  # Return original filter (may be None or have non-must conditions)
    return qdrant_models.Filter(must=conditions)
