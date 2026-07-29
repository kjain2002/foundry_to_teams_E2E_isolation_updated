"""Tiny conversationId -> threadId store.

Uses Azure Table Storage with the container's managed identity when
THREAD_TABLE_URL is set; otherwise an in-memory dict (fine for dev / single
replica testing, NOT for production with min-replicas > 1).
"""
from __future__ import annotations

import logging
from typing import Optional

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables.aio import TableClient
from azure.identity.aio import DefaultAzureCredential

from .config import settings

log = logging.getLogger(__name__)


class ThreadStore:
    async def get(self, conversation_id: str) -> Optional[str]: ...
    async def put(self, conversation_id: str, thread_id: str) -> None: ...
    async def close(self) -> None: ...


class _MemoryStore(ThreadStore):
    def __init__(self) -> None:
        self._d: dict[str, str] = {}

    async def get(self, conversation_id: str) -> Optional[str]:
        return self._d.get(conversation_id)

    async def put(self, conversation_id: str, thread_id: str) -> None:
        self._d[conversation_id] = thread_id

    async def close(self) -> None:  # noqa: D401
        return None


class _TableStore(ThreadStore):
    PARTITION = "conv"

    def __init__(self, account_url: str, table_name: str) -> None:
        self._cred = DefaultAzureCredential()
        self._client = TableClient(
            endpoint=account_url, table_name=table_name, credential=self._cred
        )

    async def _ensure_table(self) -> None:
        try:
            await self._client.create_table()
        except ResourceExistsError:
            pass

    async def get(self, conversation_id: str) -> Optional[str]:
        try:
            entity = await self._client.get_entity(
                partition_key=self.PARTITION, row_key=_safe_key(conversation_id)
            )
            return entity.get("threadId")
        except ResourceNotFoundError:
            return None

    async def put(self, conversation_id: str, thread_id: str) -> None:
        await self._ensure_table()
        await self._client.upsert_entity(
            {
                "PartitionKey": self.PARTITION,
                "RowKey": _safe_key(conversation_id),
                "threadId": thread_id,
            }
        )

    async def close(self) -> None:
        await self._client.close()
        await self._cred.close()


def _safe_key(s: str) -> str:
    # Table Storage row keys disallow '/', '\\', '#', '?', control chars.
    return s.replace("/", "_").replace("\\", "_").replace("#", "_").replace("?", "_")[:512]


def build_store() -> ThreadStore:
    if settings.thread_table_url:
        log.info("Using Azure Table thread store at %s", settings.thread_table_url)
        return _TableStore(settings.thread_table_url, settings.thread_table_name)
    log.warning("THREAD_TABLE_URL not set — using in-memory thread store (dev only)")
    return _MemoryStore()


# ---------------------------------------------------------------------------
# Per-user token store (for the in-container OAuth Authorization-Code flow)
# ---------------------------------------------------------------------------


class UserTokenStore:
    async def get(self, user_id: str) -> Optional[dict]: ...
    async def put(self, user_id: str, data: dict) -> None: ...
    async def close(self) -> None: ...


class _MemoryUserTokenStore(UserTokenStore):
    def __init__(self) -> None:
        self._d: dict[str, dict] = {}

    async def get(self, user_id: str) -> Optional[dict]:
        return self._d.get(user_id)

    async def put(self, user_id: str, data: dict) -> None:
        self._d[user_id] = data

    async def close(self) -> None:
        return None


class _TableUserTokenStore(UserTokenStore):
    PARTITION = "usr"

    def __init__(self, account_url: str, table_name: str) -> None:
        self._cred = DefaultAzureCredential()
        self._client = TableClient(
            endpoint=account_url, table_name=table_name, credential=self._cred
        )

    async def _ensure_table(self) -> None:
        try:
            await self._client.create_table()
        except ResourceExistsError:
            pass

    async def get(self, user_id: str) -> Optional[dict]:
        try:
            e = await self._client.get_entity(
                partition_key=self.PARTITION, row_key=_safe_key(user_id)
            )
            return {
                "access_token": e.get("access_token"),
                "refresh_token": e.get("refresh_token"),
                "expires_at": e.get("expires_at"),
            }
        except ResourceNotFoundError:
            return None

    async def put(self, user_id: str, data: dict) -> None:
        await self._ensure_table()
        await self._client.upsert_entity(
            {
                "PartitionKey": self.PARTITION,
                "RowKey": _safe_key(user_id),
                "access_token": data.get("access_token", ""),
                "refresh_token": data.get("refresh_token", ""),
                "expires_at": float(data.get("expires_at", 0)),
            }
        )

    async def close(self) -> None:
        await self._client.close()
        await self._cred.close()


def build_user_token_store() -> UserTokenStore:
    if settings.thread_table_url:
        return _TableUserTokenStore(
            settings.thread_table_url, settings.user_token_table_name
        )
    log.warning("THREAD_TABLE_URL not set — using in-memory user-token store (dev only)")
    return _MemoryUserTokenStore()
