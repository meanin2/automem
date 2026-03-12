"""Cross-encoder reranking for AutoMem recall results.

Uses flashrank (ONNX-based) for lightweight, GPU-free reranking.
Degrades gracefully if flashrank is not installed.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_reranker_instance: Optional[Any] = None
_reranker_model_name: Optional[str] = None
_flashrank_available: Optional[bool] = None


def _check_flashrank() -> bool:
    global _flashrank_available
    if _flashrank_available is None:
        try:
            import flashrank  # noqa: F401
            _flashrank_available = True
        except ImportError:
            _flashrank_available = False
            logger.info("flashrank not installed; reranking disabled")
    return _flashrank_available


def get_reranker(model_name: str = "ms-marco-MiniLM-L-12-v2") -> Optional[Any]:
    """Get or create a singleton flashrank Ranker instance.

    If *model_name* differs from the previously cached model, the old
    instance is discarded and a new one is created.
    """
    global _reranker_instance, _reranker_model_name
    if not _check_flashrank():
        return None
    if _reranker_instance is not None and _reranker_model_name == model_name:
        return _reranker_instance
    try:
        from flashrank import Ranker
        _reranker_instance = Ranker(model_name=model_name)
        _reranker_model_name = model_name
        logger.info("Initialized flashrank reranker with model: %s", model_name)
        return _reranker_instance
    except Exception:
        logger.exception("Failed to initialize flashrank reranker")
        return None


def rerank_results(
    query: str,
    results: List[Dict[str, Any]],
    model_name: str = "ms-marco-MiniLM-L-12-v2",
    top_k: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Rerank recall results using a cross-encoder model.

    Args:
        query: The search query text.
        results: List of recall result dicts (each must have a 'memory' dict with 'content').
        model_name: The flashrank model to use.
        top_k: Maximum results to return after reranking. None = return all.

    Returns:
        Reranked list of result dicts with 'rerank_score' added to each.
        Falls back to original order if reranking fails.
    """
    if not results or not query:
        return results

    ranker = get_reranker(model_name)
    if ranker is None:
        return results

    try:
        from flashrank import RerankRequest

        # Build passages from result content
        passages = []
        valid_indices = []
        for i, r in enumerate(results):
            content = ""
            mem = r.get("memory") or {}
            if isinstance(mem, dict):
                content = str(mem.get("content") or "")
            if not content:
                content = str(r.get("content") or r.get("id") or "")
            passages.append({"id": i, "text": content, "meta": {"index": i}})
            valid_indices.append(i)

        if not passages:
            return results

        rerank_request = RerankRequest(query=query, passages=passages)
        reranked = ranker.rerank(rerank_request)

        # Map scores back to original results
        score_map: Dict[int, float] = {}
        for item in reranked:
            idx = item["meta"]["index"]
            score_map[idx] = float(item["score"])

        # Add rerank_score to each result
        for i, r in enumerate(results):
            r["rerank_score"] = score_map.get(i, 0.0)

        # Sort by rerank_score descending
        reranked_results = sorted(results, key=lambda r: -r.get("rerank_score", 0.0))

        if top_k is not None and top_k > 0:
            reranked_results = reranked_results[:top_k]

        return reranked_results

    except Exception:
        logger.exception("Reranking failed, returning original order")
        return results
