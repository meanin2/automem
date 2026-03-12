"""AutoMem Python client library.

Provides:
    AutoMemClient - Direct API client for store, recall, update, delete operations.
    AutoMemLLMWrapper - Wraps OpenAI client with automatic memory recall and storage.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


class AutoMemClient:
    """Direct AutoMem API client."""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        bank: str = "default",
        timeout: float = 30.0,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.bank = bank
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        url = f"{self.endpoint}/{path.lstrip('/')}"
        resp = requests.request(
            method, url, headers=self._headers(), timeout=self.timeout, **kwargs
        )
        resp.raise_for_status()
        return resp.json()

    def store(
        self,
        content: str,
        tags: Optional[List[str]] = None,
        importance: Optional[float] = None,
        type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        bank: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Store a memory."""
        body: Dict[str, Any] = {"content": content, "bank": bank or self.bank}
        if tags:
            body["tags"] = tags
        if importance is not None:
            body["importance"] = importance
        if type:
            body["type"] = type
        if metadata:
            body["metadata"] = metadata
        body.update(kwargs)
        return self._request("POST", "memory", json=body)

    def recall(
        self,
        query: str,
        limit: int = 5,
        max_tokens: int = 0,
        rerank: bool = False,
        bank: Optional[str] = None,
        tags: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Recall memories matching a query."""
        params: Dict[str, Any] = {
            "query": query,
            "limit": limit,
            "bank": bank or self.bank,
        }
        if max_tokens > 0:
            params["max_tokens"] = max_tokens
        if rerank:
            params["rerank"] = "true"
        if tags:
            params["tags"] = tags
        params.update(kwargs)
        return self._request("GET", "recall", params=params)

    def update(self, memory_id: str, **kwargs: Any) -> Dict[str, Any]:
        """Update a memory."""
        return self._request("PATCH", f"memory/{memory_id}", json=kwargs)

    def delete(self, memory_id: str) -> Dict[str, Any]:
        """Delete a memory."""
        return self._request("DELETE", f"memory/{memory_id}")

    def associate(
        self,
        memory1_id: str,
        memory2_id: str,
        type: str = "RELATES_TO",
        strength: float = 0.5,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Create an association between two memories."""
        body: Dict[str, Any] = {
            "memory1_id": memory1_id,
            "memory2_id": memory2_id,
            "type": type,
            "strength": strength,
        }
        body.update(kwargs)
        return self._request("POST", "associate", json=body)

    def health(self) -> Dict[str, Any]:
        """Check service health."""
        return self._request("GET", "health")

    def recall_text(
        self,
        query: str,
        limit: int = 5,
        max_tokens: int = 0,
        **kwargs: Any,
    ) -> str:
        """Recall memories and return as formatted text."""
        data = self.recall(query, limit=limit, max_tokens=max_tokens, **kwargs)
        lines = []
        for r in data.get("results", []):
            mem = r.get("memory", {})
            content = mem.get("content", "")
            tags = mem.get("tags", [])
            tag_str = f" [{', '.join(tags)}]" if tags else ""
            lines.append(f"- {content}{tag_str}")
        return "\n".join(lines)


class AutoMemLLMWrapper:
    """Wraps an OpenAI client with automatic memory recall and storage.

    Usage:
        from openai import OpenAI
        from automem.client import AutoMemClient, AutoMemLLMWrapper

        openai_client = OpenAI()
        mem = AutoMemClient("https://automem.example.com", api_key="token")
        wrapper = AutoMemLLMWrapper(openai_client, mem)

        # Use wrapper.create() instead of openai_client.chat.completions.create()
        response = wrapper.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "What did we decide about the database?"}]
        )
    """

    def __init__(
        self,
        openai_client: Any,
        automem: AutoMemClient,
        auto_recall: bool = True,
        auto_store: bool = True,
        recall_limit: int = 5,
        max_context_tokens: int = 4000,
        system_prefix: str = "[Recalled Context]\n",
        bank: Optional[str] = None,
    ):
        self.openai = openai_client
        self.automem = automem
        self.auto_recall = auto_recall
        self.auto_store = auto_store
        self.recall_limit = recall_limit
        self.max_context_tokens = max_context_tokens
        self.system_prefix = system_prefix
        self.bank = bank or automem.bank

    def _extract_user_text(self, messages: List[Dict[str, Any]]) -> str:
        """Extract the last user message text for recall query."""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    # Handle multi-part messages
                    parts = [
                        p.get("text", "")
                        for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    ]
                    return " ".join(parts)
        return ""

    def _inject_context(
        self, messages: List[Dict[str, Any]], context: str
    ) -> List[Dict[str, Any]]:
        """Inject recalled memories as a system message."""
        if not context:
            return messages

        context_msg = {
            "role": "system",
            "content": f"{self.system_prefix}{context}",
        }

        # Insert after existing system messages, before user messages
        result = []
        inserted = False
        for msg in messages:
            if not inserted and msg.get("role") != "system":
                result.append(context_msg)
                inserted = True
            result.append(msg)
        if not inserted:
            result.append(context_msg)
        return result

    def create(self, messages: List[Dict[str, Any]], **kwargs: Any) -> Any:
        """Create a chat completion with automatic memory recall and storage.

        Drop-in replacement for openai_client.chat.completions.create().
        """
        user_text = self._extract_user_text(messages)

        # Auto-recall relevant memories
        if self.auto_recall and user_text:
            try:
                context = self.automem.recall_text(
                    query=user_text,
                    limit=self.recall_limit,
                    max_tokens=self.max_context_tokens,
                    bank=self.bank,
                )
                if context.strip():
                    messages = self._inject_context(list(messages), context)
            except Exception:
                logger.warning("Memory recall failed, proceeding without context", exc_info=True)

        # Call the underlying OpenAI client
        response = self.openai.chat.completions.create(messages=messages, **kwargs)

        # Auto-store the exchange (with dedup check)
        if self.auto_store and user_text:
            try:
                assistant_text = ""
                if hasattr(response, "choices") and response.choices:
                    msg = response.choices[0].message
                    assistant_text = getattr(msg, "content", "") or ""

                if assistant_text:
                    exchange = f"User: {user_text}\nAssistant: {assistant_text}"
                    # Truncate to reasonable size
                    if len(exchange) > 1500:
                        exchange = exchange[:1500] + "..."

                    # Dedup: skip if a very similar memory already exists
                    skip_store = False
                    try:
                        existing = self.automem.recall(
                            query=user_text,
                            limit=1,
                            bank=self.bank,
                        )
                        for r in existing.get("results", []):
                            similarity = float(r.get("score", 0.0))
                            if similarity > 0.95:
                                skip_store = True
                                break
                    except Exception:
                        # If recall fails, proceed with storing
                        pass

                    if not skip_store:
                        self.automem.store(
                            content=exchange,
                            tags=["llm-exchange", "auto-stored"],
                            importance=0.4,
                            type="Context",
                            bank=self.bank,
                        )
            except Exception:
                logger.warning("Failed to auto-store exchange", exc_info=True)

        return response
