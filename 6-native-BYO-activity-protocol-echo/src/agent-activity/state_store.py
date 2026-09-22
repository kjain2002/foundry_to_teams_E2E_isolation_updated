# Copyright (c) Microsoft. All rights reserved.

"""Durable per-user/per-conversation state in Azure Blob.

Replaces in-process dicts so preferences, the 'asked' flag, and conversation
history survive container cold starts, redeploys, and multiple replicas. Uses
the agent's managed identity (DefaultAzureCredential) against the project's
storage account over its private endpoint - no keys or connection strings.
"""

from __future__ import annotations

import json
import logging

from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

import config

logger = logging.getLogger("agent-activity.state")

_CONTAINER = "agent-state"
_svc: BlobServiceClient | None = None
_container_ready = False


def _service() -> BlobServiceClient:
    global _svc
    if _svc is None:
        url = f"https://{config.STATE_STORAGE_ACCOUNT}.blob.core.windows.net"
        _svc = BlobServiceClient(account_url=url, credential=DefaultAzureCredential())
    return _svc


def _container_client():
    global _container_ready
    svc = _service()
    if not _container_ready:
        try:
            svc.create_container(_CONTAINER)
        except Exception:  # pylint: disable=broad-exception-caught  # exists or benign race
            pass
        _container_ready = True
    return svc.get_container_client(_CONTAINER)


def _blob_name(prefix: str, key: str) -> str:
    return f"{prefix}/{key.replace('/', '_')}.json"


def get_json(prefix: str, key: str) -> dict | None:
    try:
        blob = _container_client().get_blob_client(_blob_name(prefix, key))
        return json.loads(blob.download_blob().readall())
    except ResourceNotFoundError:
        return None
    except Exception:  # pylint: disable=broad-exception-caught
        logger.warning("state get failed %s/%s", prefix, key, exc_info=True)
        return None


def put_json(prefix: str, key: str, value: dict) -> None:
    try:
        blob = _container_client().get_blob_client(_blob_name(prefix, key))
        blob.upload_blob(json.dumps(value).encode("utf-8"), overwrite=True)
    except Exception:  # pylint: disable=broad-exception-caught
        logger.warning("state put failed %s/%s", prefix, key, exc_info=True)


def delete_json(prefix: str, key: str) -> None:
    try:
        blob = _container_client().get_blob_client(_blob_name(prefix, key))
        blob.delete_blob()
    except ResourceNotFoundError:
        pass
    except Exception:  # pylint: disable=broad-exception-caught
        logger.warning("state delete failed %s/%s", prefix, key, exc_info=True)
