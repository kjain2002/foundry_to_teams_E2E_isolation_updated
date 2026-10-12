# Copyright (c) Microsoft. All rights reserved.
"""Deterministically extract a target agent's definition from Foundry.

Run this ONCE whenever you want to (re)link a target agent. It fetches the
target agent's model + instructions + tool schemas from the Foundry project and
writes them to ``src/activity-agent-2/target-agent.yaml`` (the file the helper
reads at startup) plus a raw ``target-agent.schema.json`` for transparency. It is
fully repeatable: same inputs → same output, every run. No agent/LLM involved.

Usage (PowerShell), signed into the target's tenant/subscription::

    python fetch_target_agent.py --agent my-target-agent `
        --endpoint https://<foundry-resource-name>.services.ai.azure.com/api/projects/<foundry-project-name>

Or rely on env vars (TARGET_AGENT_ID, FOUNDRY_PROJECT_ENDPOINT)::

    python fetch_target_agent.py

Auth: uses DefaultAzureCredential (Azure CLI / managed identity) to mint an
``https://ai.azure.com/.default`` token — the same audience the helper uses at
runtime. You need at least read access to the target agent in the project.

Requires: httpx, azure-identity, PyYAML (all already in requirements.txt).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import yaml
from azure.identity import DefaultAzureCredential

_API_VERSION = "v1"
_SCOPE = "https://ai.azure.com/.default"
_OUT_DIR = Path(__file__).resolve().parent / "src" / "activity-agent-2"


def _token() -> str:
    return DefaultAzureCredential().get_token(_SCOPE).token


def _fetch_agent(endpoint: str, agent: str, token: str) -> dict[str, Any]:
    url = f"{endpoint.rstrip('/')}/agents/{agent}?api-version={_API_VERSION}"
    last: Exception | None = None
    for attempt in range(1, 4):
        try:
            resp = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            # 4xx is a real error (auth/not-found); only retry 5xx.
            if exc.response.status_code < 500:
                raise
            last = exc
        except httpx.TransportError as exc:
            # Transient DNS/connect/TLS resets — the network can flap.
            last = exc
        print(f"  [retry {attempt}/3] {type(last).__name__}: {last}")
        time.sleep(2 * attempt)
    raise last  # type: ignore[misc]


def _extract_definition(doc: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Find the agent's definition block across the possible response shapes."""
    versions = doc.get("versions")
    if isinstance(versions, dict):
        latest = versions.get("latest")
        if isinstance(latest, dict) and isinstance(latest.get("definition"), dict):
            return latest["definition"], latest.get("version")
    if isinstance(doc.get("definition"), dict):
        return doc["definition"], doc.get("version")
    return doc, None


def _mcp_tool_schemas(server_url: str, token: str) -> list[dict[str, Any]]:
    """Best-effort: list an MCP server's tools so their schemas are captured.

    Non-fatal — if the handshake fails the SDK still discovers tools at runtime.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    init = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "fetch_target_agent", "version": "1.0"},
        },
    }
    list_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}

    def _parse(body: str) -> dict[str, Any] | None:
        body = body.strip()
        if not body:
            return None
        if body.startswith("{"):
            return json.loads(body)
        # SSE framing: find the last 'data:' line.
        for line in reversed(body.splitlines()):
            line = line.strip()
            if line.startswith("data:"):
                return json.loads(line[len("data:"):].strip())
        return None

    try:
        with httpx.Client(timeout=60, headers=headers) as client:
            r1 = client.post(server_url, json=init)
            r1.raise_for_status()
            session_header = r1.headers.get("mcp-session-id")
            h2 = {"mcp-session-id": session_header} if session_header else {}
            r2 = client.post(server_url, json=list_req, headers=h2)
            r2.raise_for_status()
            parsed = _parse(r2.text)
            if parsed and isinstance(parsed.get("result"), dict):
                return parsed["result"].get("tools", []) or []
    except Exception as exc:  # pylint: disable=broad-exception-caught
        print(f"  [warn] could not list MCP tools for {server_url}: {exc}")
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract a Foundry target agent definition.")
    parser.add_argument("--agent", default=os.environ.get("TARGET_AGENT_ID", ""),
                        help="Target agent name/id (or env TARGET_AGENT_ID).")
    parser.add_argument("--endpoint", default=os.environ.get("FOUNDRY_PROJECT_ENDPOINT", ""),
                        help="Foundry project endpoint (or env FOUNDRY_PROJECT_ENDPOINT).")
    parser.add_argument("--out-dir", default=str(_OUT_DIR),
                        help="Where to write target-agent.yaml / target-agent.schema.json.")
    parser.add_argument("--no-mcp-schemas", action="store_true",
                        help="Skip the best-effort MCP tools/list enrichment.")
    args = parser.parse_args()

    if not args.agent or not args.endpoint:
        print("ERROR: provide --agent and --endpoint (or set TARGET_AGENT_ID and "
              "FOUNDRY_PROJECT_ENDPOINT).", file=sys.stderr)
        return 2

    print(f"Fetching agent '{args.agent}' from {args.endpoint} ...")
    token = _token()
    doc = _fetch_agent(args.endpoint, args.agent, token)
    definition, version = _extract_definition(doc)

    model = definition.get("model")
    instructions = definition.get("instructions") or ""
    tools = [t for t in (definition.get("tools") or []) if isinstance(t, dict)]

    # Enrich MCP tools with their live tool schemas (best-effort, for transparency).
    mcp_schemas: dict[str, list[dict[str, Any]]] = {}
    if not args.no_mcp_schemas:
        for tool in tools:
            if str(tool.get("type", "")).lower() == "mcp" and tool.get("server_url"):
                label = tool.get("server_label") or "mcp"
                print(f"  listing MCP tools for '{label}' ...")
                mcp_schemas[label] = _mcp_tool_schemas(tool["server_url"], token)

    target = {
        "name": doc.get("name") or args.agent,
        "model": model,
        "instructions": instructions,
        "tools": tools,
    }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    yaml_path = out_dir / "target-agent.yaml"
    schema_path = out_dir / "target-agent.schema.json"

    # Remove a stale .yml so the generated .yaml is the single source of truth.
    stale = out_dir / "target-agent.yml"
    if stale.exists():
        stale.unlink()

    header = (
        f"# GENERATED by fetch_target_agent.py — do not edit by hand.\n"
        f"# Source: {args.agent} (version {version}) @ {args.endpoint}\n"
    )
    with yaml_path.open("w", encoding="utf-8") as fh:
        fh.write(header)
        yaml.safe_dump(target, fh, sort_keys=False, default_flow_style=False, allow_unicode=True)

    raw = {"agent": doc, "definition": definition, "version": version, "mcp_tool_schemas": mcp_schemas}
    schema_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nWrote {yaml_path}")
    print(f"Wrote {schema_path}")
    print(f"  model={model!r} instructions_len={len(instructions)} tools={len(tools)}")
    for label, schemas in mcp_schemas.items():
        print(f"  mcp '{label}': {len(schemas)} tool schema(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
