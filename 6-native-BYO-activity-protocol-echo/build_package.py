"""Build a sideload-ready Teams app package for the agent-activity bot with
supportsFiles enabled. Points at the SAME bot (botId = the agent MI app id), so
it routes to the same hosted agent + durable state - it only adds Teams
personal-chat file upload/consent (which the Foundry auto-publish omits).

Usage:
  # Set two env vars (or edit the defaults below):
  $env:BOT_ID = "<agent-instance-identity-client-id>"
  $env:APP_ID = "<a-guid-for-your-teams-app-id-stable-across-runs>"
  python build_package.py

Then in Teams: Apps > Manage your apps > Upload a custom app > agent-activity-teams.zip
"""

import json
import os
import struct
import uuid
import zipfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(HERE, "teams-app-package")
os.makedirs(PKG, exist_ok=True)

# The agent's instance_identity.client_id - the same value used as the bot's
# msaAppId in bot-service.bicep. Fetch it with:
#   az rest --method get --url "<project-endpoint>/agents/<agent>?api-version=v1" \
#     --resource https://ai.azure.com --query instance_identity.client_id -o tsv
BOT_ID = os.environ.get("BOT_ID", "<agent-instance-identity-client-id>")

# A stable GUID for your Teams app. Re-runs with the same APP_ID will UPDATE
# the same installed app; changing it creates a new app entry.
APP_ID = os.environ.get("APP_ID") or str(uuid.uuid4())

manifest = {
    "$schema": "https://developer.microsoft.com/en-us/json-schemas/teams/v1.17/MicrosoftTeams.schema.json",
    "manifestVersion": "1.17",
    "version": "1.0.0",
    "id": APP_ID,
    "developer": {
        "name": os.environ.get("DEVELOPER_NAME", "<your-org>"),
        "websiteUrl": os.environ.get("DEVELOPER_WEBSITE", "https://www.example.com"),
        "privacyUrl": os.environ.get("DEVELOPER_PRIVACY", "https://www.example.com/privacy"),
        "termsOfUseUrl": os.environ.get("DEVELOPER_TERMS", "https://www.example.com/terms"),
    },
    "name": {
        "short": os.environ.get("APP_NAME_SHORT", "Activity Agent+"),
        "full": os.environ.get("APP_NAME_FULL", "Activity Agent (files enabled)"),
    },
    "description": {
        "short": os.environ.get("APP_DESC_SHORT", "Foundry activity agent with file upload enabled."),
        "full": os.environ.get(
            "APP_DESC_FULL",
            "Activity-protocol helper for a Foundry hosted agent, with Teams personal-chat file upload and consent enabled (supportsFiles).",
        ),
    },
    "icons": {"outline": "outline.png", "color": "color.png"},
    "accentColor": "#0078D4",
    "bots": [
        {
            "botId": BOT_ID,
            "scopes": ["personal", "team", "groupChat"],
            "supportsFiles": True,
            "isNotificationOnly": False,
            "commandLists": [
                {
                    "scopes": ["personal"],
                    "commands": [
                        {"title": "help", "description": "How to use this agent"}
                    ],
                }
            ],
        }
    ],
    "permissions": ["identity", "messageTeamMembers"],
    "validDomains": [],
    "webApplicationInfo": {
        "id": BOT_ID,
        "resource": f"api://botid-{BOT_ID}",
    },
}

with open(os.path.join(PKG, "manifest.json"), "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2)


def _solid_png(path: str, w: int, h: int, rgba: tuple[int, int, int, int]) -> None:
    """Write a minimal valid solid-color RGBA PNG (no external deps)."""
    def _chunk(typ: bytes, data: bytes) -> bytes:
        body = typ + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)  # 8-bit RGBA
    row = bytes(rgba) * w
    raw = bytearray()
    for _ in range(h):
        raw.append(0)  # filter: none
        raw += row
    idat = zlib.compress(bytes(raw))
    with open(path, "wb") as f:
        f.write(sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b""))


def _outline_png(path: str, size: int = 32) -> None:
    """32x32 transparent PNG with a centered white square (Teams outline rule)."""
    def _chunk(typ: bytes, data: bytes) -> bytes:
        body = typ + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    inset = size // 4
    transparent = bytes((0, 0, 0, 0))
    white = bytes((255, 255, 255, 255))
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            if inset <= x < size - inset and inset <= y < size - inset:
                raw += white
            else:
                raw += transparent
    idat = zlib.compress(bytes(raw))
    with open(path, "wb") as f:
        f.write(sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b""))


_solid_png(os.path.join(PKG, "color.png"), 192, 192, (0, 120, 212, 255))  # opaque brand blue
_outline_png(os.path.join(PKG, "outline.png"), 32)                        # transparent + white shape

zip_path = os.path.join(HERE, "agent-activity-teams.zip")
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
    for name in ("manifest.json", "color.png", "outline.png"):
        z.write(os.path.join(PKG, name), name)

print("APP_ID:", APP_ID)
print("BOT_ID:", BOT_ID)
print("package:", zip_path)
