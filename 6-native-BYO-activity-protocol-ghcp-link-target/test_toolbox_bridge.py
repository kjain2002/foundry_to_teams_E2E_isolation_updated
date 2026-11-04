import json
import asyncio
import ast
import io
import importlib.util
import sys
import tempfile
import unittest
import types
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).parent / "src" / "activity-agent"))

import fetch_target_agent
import toolbox
import target_agent
import files as shared_file_store
import auth_retry


class ToolboxBridgeTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.mode = "direct"
        self.failed_skills = False
        self.failed_auth_call = False
        self.default_version = "5"
        self.get_requests = []
        self.skill_text = "Skill instructions"
        self.servers = [
            {"label": "sql", "url": "https://first.example/mcp", "require_approval": "never"},
            {"label": "docs", "url": "https://second.example/mcp", "require_approval": "never"},
        ]
        real_client = httpx.Client
        for active_patch in (
            patch.object(toolbox, "_SERVERS", self.servers),
            patch.object(toolbox, "_SKILLS_CACHE", {}),
            patch.object(toolbox, "_SKILLS_FETCHED_AT", {}),
            patch.object(toolbox, "_token", return_value="test-token"),
            patch.object(httpx, "Client", side_effect=lambda **kwargs: real_client(
                transport=httpx.MockTransport(self.respond), **kwargs)),
        ):
            active_patch.start()
            self.addCleanup(active_patch.stop)

    def respond(self, request):
        if request.method == "GET":
            self.get_requests.append(str(request.url))
            if self.mode == "manifest_denied":
                return httpx.Response(403)
            if "/versions/" not in request.url.path:
                return httpx.Response(200, json={"default_version": self.default_version,
                                                "versions": {"latest": {"version": "99"}}})
            version = request.url.path.split("/versions/")[1]
            tools = [
                {"type": "code_interpreter", "name": "code_interpreter"},
                {"type": "mcp", "server_label": "documents", "project_connection_id": "documents"},
                {"type": "mcp", "server_label": "starburst", "project_connection_id": "starburst"},
            ]
            if version == "6":
                tools.append({"type": "mcp", "server_label": "diagnostic-mcp", "project_connection_id": "diagnostic-mcp"})
            return httpx.Response(200, json={"version": version, "tools": tools})
        payload = json.loads(request.content)
        self.requests.append((request, payload))
        method = payload["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {"capabilities": {}}
        elif method == "tools/list":
            if self.mode == "consent" or (self.mode in ("http_401", "embedded_401", "sql_http_401", "sql_embedded_401") and self.failed_auth_call):
                return httpx.Response(200, json={"error": {
                    "code": -32006,
                    "message": "CONSENT_REQUIRED https://consent.example/login",
                }})
            if self.mode in ("facade", "mixed_gateway", "source_consent", "source_error", "manifest_denied", "401_gateway"):
                result = {"tools": [{"name": "tool_search", "inputSchema": {
                    "type": "object", "properties": {"limit": {"type": "integer", "maximum": 10}},
                }}, {"name": "call_tool"}]}
                if self.mode == "mixed_gateway":
                    result["tools"].append({"name": "diagnostic-mcp___getProbeIdentity"})
            elif self.mode in ("sql", "sql_http_401", "sql_embedded_401"):
                result = {"tools": [{"name": "execute_query", "inputSchema": {
                    "type": "object", "properties": {"sql": {"type": "string"}},
                }}]}
            elif self.mode == "pages" and not payload["params"].get("cursor"):
                result = {"tools": [{"name": "first_page"}], "nextCursor": "next"}
            else:
                result = {"tools": [{"name": "search_documents", "inputSchema": {
                    "type": "object", "properties": {"text": {"type": "string"}},
                }}]}
        elif method == "tools/call":
            if self.mode == "401_gateway":
                if payload["params"]["name"] == "call_tool":
                    return httpx.Response(401)
                return httpx.Response(200, json={"error": {"code": -32006,
                    "message": "CONSENT_REQUIRED https://consent.example/login"}})
            if self.mode in ("http_401", "embedded_401", "401_without_consent", "sql_http_401", "sql_embedded_401"):
                self.failed_auth_call = True
                if self.mode in ("embedded_401", "sql_embedded_401"):
                    return httpx.Response(200, json={"result": {"isError": True, "content": [
                        {"type": "text", "text": '{"error":{"code":401,"message":"Unauthorized"}}'},
                    ]}})
                return httpx.Response(401)
            if payload["params"]["name"] == "tool_search":
                self.assertLessEqual(payload["params"]["arguments"]["limit"], 10)
                query = payload["params"]["arguments"]["query"]
                if query == "starburst" and self.mode == "source_error":
                    return httpx.Response(503)
                if query == "starburst" and self.mode == "source_consent":
                    return httpx.Response(200, json={"error": {
                        "code": -32006, "message": "CONSENT_REQUIRED https://consent.example/login",
                    }})
                tool_name = "starburst___executeQuery" if query == "starburst" else "documents.search"
                result = {"tools": [{"name": tool_name, "inputSchema": {
                    "type": "object", "properties": {"text": {"type": "string"}},
                }}]}
            else:
                result = {"content": [{"type": "text", "text": "Found document"}]}
        elif method == "skills/list":
            if self.failed_skills and request.url.host == "second.example":
                return httpx.Response(503)
            result = {"skills": [{
                "uri": "skill://summary/SKILL.md", "frontmatter": {"name": "summary", "description": "Summarize"},
                "resources": ["skill://summary/reference.md"],
            }]}
        elif method == "resources/read":
            result = {"contents": [{"uri": payload["params"]["uri"], "text": self.skill_text}]}
        else:
            return httpx.Response(200, json={"error": {"code": -32601, "message": "Unknown method"}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
                              headers={"mcp-session-id": "test-session"})

    def test_foundry_reference_follows_default_and_uses_actual_name(self):
        original = {
            "type": "mcp", "server_label": "connection-alias", "project_connection_id": "connection-alias",
            "server_url": "https://account.services.ai.azure.com/api/projects/test/toolboxes/actual-toolbox/versions/3/mcp?api-version=v1",
        }
        normalized = target_agent.normalize_toolbox_reference(original)
        self.assertEqual(normalized["server_label"], "actual-toolbox")
        self.assertEqual(normalized["project_connection_id"], "connection-alias")
        self.assertEqual(normalized["server_url"],
                         "https://account.services.ai.azure.com/api/projects/test/toolboxes/actual-toolbox/mcp?api-version=v1")
        self.assertIn("/versions/3/", original["server_url"])
        target = target_agent._from_mapping({"tools": [original]}, "test")
        self.assertEqual(target.mcp_servers[0]["label"], "actual-toolbox")
        self.assertEqual(target.mcp_servers[0]["url"], normalized["server_url"])

    def test_non_foundry_mcp_reference_is_not_rewritten(self):
        original = {"type": "mcp", "server_label": "external", "server_url":
                    "https://external.example/api/projects/test/toolboxes/actual/versions/3/mcp?api-version=v1"}
        self.assertEqual(target_agent.normalize_toolbox_reference(original), original)

    def test_search_reaches_all_servers_and_includes_non_sql_tools(self):
        results = [toolbox.toolbox_request(key, query="find documents") for key in toolbox.toolbox_servers()]
        self.assertEqual([result["status"] for result in results], ["ok", "ok"])
        self.assertEqual(results[1]["result"]["tools"][0]["name"], "search_documents")
        self.assertEqual({request.url.host for request, _ in self.requests}, {"first.example", "second.example"})

    def test_non_sql_call_preserves_arguments_and_user_context(self):
        result = toolbox.toolbox_request("docs", name="search_documents", arguments={"text": "report"}, call_id="user-call")
        self.assertEqual(result["status"], "ok")
        request, payload = self.requests[-1]
        self.assertEqual(payload["params"]["arguments"], {"text": "report"})
        self.assertEqual(request.headers["x-agent-foundry-call-id"], "user-call")
        self.assertEqual(request.headers["mcp-session-id"], "test-session")

    def test_facade_uses_actual_query_and_nested_call(self):
        self.mode = "facade"
        result = toolbox.toolbox_request("docs", query="find documents")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.requests[-1][1]["params"]["arguments"]["query"], "find documents")
        self.assertFalse(result["inventory_complete"])
        self.assertIn("not a complete", result["discovery_note"])
        result = toolbox.toolbox_request("docs", name="documents.search", arguments={"text": "report"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.requests[-1][1]["params"], {
            "name": "call_tool", "arguments": {"name": "documents.search", "arguments": {"text": "report"}},
        })

    def test_mixed_gateway_invokes_tool_not_in_initial_list(self):
        self.mode = "mixed_gateway"
        arguments = {"body": {"sqlQuery": "SELECT 1"}}
        with self.assertLogs(toolbox.logger, level="INFO") as logs:
            result = toolbox.toolbox_request("docs", name="starburst___executeQuery",
                                                   arguments=arguments, call_id="user-call")
        self.assertEqual(result["status"], "ok")
        self.assertTrue(any("via=call_tool" in message for message in logs.output))
        self.assertTrue(any("TOOLBOX_CALL_COMPLETE" in message for message in logs.output))
        self.assertFalse(any("SELECT 1" in message for message in logs.output))
        request, payload = self.requests[-1]
        self.assertEqual(payload["params"], {"name": "call_tool", "arguments": {
            "name": "starburst___executeQuery", "arguments": arguments,
        }})
        self.assertEqual(request.headers["x-agent-foundry-call-id"], "user-call")

    def test_mixed_gateway_inventory_includes_configured_code_interpreter(self):
        self.mode = "mixed_gateway"
        self.servers[1]["url"] = "https://second.example/api/projects/test/toolboxes/docs/mcp?api-version=v1"
        result = toolbox.toolbox_inventory("docs", call_id="user-call")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["builtin_tools"], [{"name": "code_interpreter", "type": "code_interpreter"}])
        self.assertIn("starburst", [source["source"] for source in result["configured_mcp_sources"]])
        self.assertIn("must be shown", result["discovery_note"])

    def test_inventory_searches_each_declared_source_and_separates_builtins(self):
        self.mode = "facade"
        self.servers[1]["url"] = "https://second.example/api/projects/test/toolboxes/docs/versions/3/mcp?api-version=v1"
        result = toolbox.toolbox_inventory("docs", call_id="user-call")
        self.assertEqual(result["status"], "ok")
        self.assertEqual([source["source"] for source in result["configured_mcp_sources"]], ["documents", "starburst"])
        self.assertEqual(result["builtin_tools"], [{"name": "code_interpreter", "type": "code_interpreter"}])
        self.assertEqual(result["source_searches"][1]["result"]["tools"][0]["name"], "starburst___executeQuery")
        queries = [payload["params"]["arguments"]["query"] for _, payload in self.requests
                   if payload["method"] == "tools/call"]
        self.assertEqual(queries, ["documents", "starburst"])
        self.assertTrue(all(request.headers["x-agent-foundry-call-id"] == "user-call"
                            for request, _ in self.requests))

    def test_consumer_inventory_tracks_published_default_not_newest_created(self):
        self.mode = "facade"
        self.servers[1]["url"] = "https://second.example/api/projects/test/toolboxes/docs/mcp?api-version=v1"
        result = toolbox.toolbox_inventory("docs", call_id="user-call")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["toolbox_version"], "5")
        self.assertTrue(any("/versions/5?" in url for url in self.get_requests))
        self.assertFalse(any("/versions/99" in url for url in self.get_requests))
        self.default_version = "6"
        result = toolbox.toolbox_inventory("docs", call_id="user-call")
        self.assertEqual(result["toolbox_version"], "6")
        self.assertIn("diagnostic-mcp", [source["source"] for source in result["configured_mcp_sources"]])

    def test_inventory_preserves_source_consent_and_other_results(self):
        self.mode = "source_consent"
        self.servers[1]["url"] = "https://second.example/api/projects/test/toolboxes/docs/versions/3/mcp?api-version=v1"
        result = toolbox.toolbox_inventory("docs", call_id="user-call")
        self.assertEqual(result["status"], "partial")
        self.assertEqual([source["status"] for source in result["source_searches"]], ["ok", "consent"])
        self.assertEqual(result["source_searches"][1]["source"], "starburst")
        self.assertEqual(len(result["configured_mcp_sources"]), 2)

    def test_inventory_denied_manifest_is_not_reported_as_empty(self):
        self.mode = "manifest_denied"
        self.servers[1]["url"] = "https://second.example/api/projects/test/toolboxes/docs/versions/3/mcp?api-version=v1"
        result = toolbox.toolbox_inventory("docs")
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["inventory_complete"])

    def test_inventory_preserves_results_when_one_source_is_unavailable(self):
        self.mode = "source_error"
        self.servers[1]["url"] = "https://second.example/api/projects/test/toolboxes/docs/versions/3/mcp?api-version=v1"
        result = toolbox.toolbox_inventory("docs")
        self.assertEqual(result["status"], "partial")
        self.assertEqual([source["status"] for source in result["source_searches"]], ["ok", "error"])
        self.assertEqual(result["source_searches"][1]["source"], "starburst")

    def test_consent_is_generic_and_keeps_source_toolbox(self):
        self.mode = "consent"
        result = toolbox.toolbox_request("docs", query="find documents", call_id="user-call")
        self.assertEqual(result, {"status": "consent", "url": "https://consent.example/login", "toolbox": "docs"})
        self.assertTrue(all(request.headers["x-agent-foundry-call-id"] == "user-call"
                            for request, _ in self.requests))

    def test_401_probes_for_real_consent_without_replaying_tool(self):
        for mode in ("http_401", "embedded_401"):
            with self.subTest(mode=mode):
                self.mode = mode
                self.failed_auth_call = False
                self.requests.clear()
                result = toolbox.toolbox_request("docs", name="search_documents",
                                                       arguments={"text": "report"}, call_id="fresh-call")
                self.assertEqual(result["status"], "consent")
                self.assertEqual(result["url"], "https://consent.example/login")
                self.assertEqual(sum(payload["method"] == "tools/call" for _, payload in self.requests), 1)
                self.assertTrue(all(request.headers["x-agent-foundry-call-id"] == "fresh-call"
                                    for request, _ in self.requests))

    def test_successful_auth_documentation_is_not_an_auth_failure(self):
        texts = ("The documentation explains how to diagnose 401 Unauthorized responses.",
                 "Unauthorized", '{"message":"401 Unauthorized","status":"ok"}')
        for is_error in (None, False):
            for text in texts:
                with self.subTest(isError=is_error, text=text):
                    result = {"content": [{"type": "text", "text": text}]}
                    if is_error is not None:
                        result["isError"] = is_error
                    response = httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})
                    response.request = httpx.Request("POST", "https://example.invalid/mcp")
                    self.assertEqual(toolbox._checked_response(response), result)

    def test_structured_and_explicit_auth_errors_are_still_detected(self):
        payloads = [
            {"error": {"code": 401, "message": "Token expired"}},
            {"error": {"code": -32000, "message": "HTTP 401 Unauthorized"}},
            {"result": {"isError": True, "content": [{"type": "text", "text": "HTTP 401"}]}},
            {"result": {"content": [{"type": "text", "text": '{"error":{"statusCode":401,"message":"Token expired"}}'}]}},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                response = httpx.Response(200, json=payload)
                response.request = httpx.Request("POST", "https://example.invalid/mcp")
                with self.assertRaises(toolbox._ToolboxFailure) as failure:
                    toolbox._checked_response(response)
                self.assertTrue(failure.exception.result.get("auth_required"))

    def test_non_auth_tool_error_does_not_trigger_auth_recovery(self):
        response = httpx.Response(200, json={"result": {"isError": True, "content": [
            {"type": "text", "text": "Service unavailable: HTTP 503"},
        ]}})
        response.request = httpx.Request("POST", "https://example.invalid/mcp")
        with self.assertRaises(toolbox._ToolboxFailure) as failure:
            toolbox._checked_response(response)
        self.assertFalse(failure.exception.result.get("auth_required"))

    def test_401_gateway_probe_searches_failed_capability_without_reinvoking(self):
        self.mode = "401_gateway"
        result = toolbox.toolbox_request("docs", name="starburst___executeQuery",
                                               arguments={"body": {"sqlQuery": "SELECT 1"}}, call_id="fresh-call")
        self.assertEqual(result["status"], "consent")
        calls = [payload["params"] for _, payload in self.requests if payload["method"] == "tools/call"]
        self.assertEqual([call["name"] for call in calls], ["call_tool", "tool_search"])
        self.assertEqual(calls[1]["arguments"]["query"], "starburst___executeQuery")

    def test_sql_compatibility_path_also_recovers_consent_after_401(self):
        for mode in ("sql_http_401", "sql_embedded_401"):
            with self.subTest(mode=mode), patch.object(toolbox, "_SERVER", self.servers[0]):
                self.mode = mode
                self.failed_auth_call = False
                result = toolbox.run_query("SHOW CATALOGS", call_id="fresh-call")
                self.assertEqual(result["status"], "consent")
                self.assertEqual(result["url"], "https://consent.example/login")

    def test_401_without_consent_url_remains_an_error(self):
        self.mode = "401_without_consent"
        result = toolbox.toolbox_request("docs", name="search_documents", arguments={"text": "report"})
        self.assertEqual(result["status"], "error")
        self.assertNotIn("url", result)
        self.assertIn("did not provide a consent URL", result["message"])

    def test_unknown_tool_does_not_execute(self):
        result = toolbox.toolbox_request("docs", name="guessed_tool", arguments={})
        self.assertEqual(result["status"], "error")
        self.assertFalse(any(payload["method"] == "tools/call" for _, payload in self.requests))

    def test_pagination_includes_later_tools(self):
        self.mode = "pages"
        result = toolbox.toolbox_request("docs", query="documents")
        self.assertEqual([tool["name"] for tool in result["result"]["tools"]], ["first_page", "search_documents"])

    def test_approval_is_not_silently_bypassed(self):
        self.servers[1]["require_approval"] = "always"
        result = toolbox.toolbox_request("docs", name="search_documents", arguments={"text": "report"})
        self.assertEqual(result["status"], "error")
        self.assertIn("approval", result["message"])

    def test_skills_from_all_toolboxes_are_namespaced_and_cached(self):
        skills = toolbox.fetch_skills()
        self.assertEqual([skill["name"] for skill in skills], ["sql/summary", "docs/summary"])
        count = len(self.requests)
        self.assertEqual(toolbox.fetch_skills(), skills)
        self.assertEqual(len(self.requests), count)
        self.assertIn("Supporting resources", toolbox.get_skill_markdown("docs/summary"))
        self.assertIn("Available toolbox skills", toolbox.get_skill_markdown("summary"))

    def test_skills_refresh_after_cache_expiry(self):
        with patch.object(toolbox.time, "monotonic", return_value=100):
            self.assertEqual(toolbox.fetch_skills()[0]["markdown"], "Skill instructions")
        self.skill_text = "Updated skill instructions"
        with patch.object(toolbox.time, "monotonic", return_value=110):
            self.assertEqual(toolbox.fetch_skills()[0]["markdown"], "Skill instructions")
        with patch.object(toolbox.time, "monotonic", return_value=161):
            self.assertEqual(toolbox.fetch_skills()[0]["markdown"], "Updated skill instructions")

    def test_failed_skills_retry_without_losing_other_toolbox(self):
        self.failed_skills = True
        self.assertEqual(len(toolbox.fetch_skills()), 1)
        self.failed_skills = False
        self.assertEqual(len(toolbox.fetch_skills()), 2)

    def test_resource_reads_do_not_require_tool_discovery(self):
        self.mode = "consent"
        result = toolbox.toolbox_request("docs", uri="skill://summary/reference.md", call_id="user-call")
        self.assertEqual(result["status"], "ok")
        self.assertFalse(any(payload["method"] == "tools/list" for _, payload in self.requests))

    def test_deploy_inventory_includes_skills_and_defers_consent(self):
        self.mode = "consent"
        result = fetch_target_agent._mcp_inventory("https://second.example/mcp", "test-token")
        self.assertTrue(result["runtime_discovery_required"])
        self.assertIn("consent required", result["warnings"][0])
        self.assertNotIn("https://consent.example", json.dumps(result))
        self.assertEqual(result["skills"][0]["markdown"], "Skill instructions")

    def test_existing_sql_bridge_still_calls_tool_and_forwards_user(self):
        self.mode = "sql"
        with patch.object(toolbox, "_SERVER", self.servers[0]):
            result = toolbox.run_query("SHOW CATALOGS", call_id="user-call")
        self.assertEqual(result["status"], "ok")
        request, payload = self.requests[-1]
        self.assertEqual(payload["params"]["arguments"], {"sql": "SHOW CATALOGS"})
        self.assertEqual(request.headers["x-agent-foundry-call-id"], "user-call")

    def test_relink_normalizes_pinned_foundry_toolbox(self):
        tool = {"type": "mcp", "server_label": "alias", "project_connection_id": "alias",
                "server_url": "https://account.services.ai.azure.com/api/projects/test/toolboxes/actual/versions/3/mcp?api-version=v1"}
        agent = {"definition": {"model": "model", "tools": [tool]}}
        with tempfile.TemporaryDirectory() as output:
            with patch.object(sys, "argv", ["fetch_target_agent.py", "--agent", "target",
                                           "--endpoint", "https://project.example", "--out-dir", output]), \
                    patch.object(fetch_target_agent, "_token", return_value="test-token"), \
                    patch.object(fetch_target_agent, "_fetch_agent", return_value=agent), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(fetch_target_agent.main(), 0)
            target = yaml.safe_load((Path(output) / "target-agent.yaml").read_text(encoding="utf-8"))
            raw = json.loads((Path(output) / "target-agent.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(target["tools"][0]["server_label"], "actual")
        self.assertNotIn("/versions/", target["tools"][0]["server_url"])
        self.assertEqual(target["tools"][0]["project_connection_id"], "alias")
        self.assertEqual(raw["definition"]["tools"][0], tool)

    def test_relink_refreshes_latest_agent_and_every_attached_endpoint(self):
        tools = [{"type": "mcp", "server_label": server["label"], "server_url": server["url"]}
                 for server in self.servers]
        agent = {"name": "target", "versions": {"latest": {
            "version": "11", "definition": {"model": "new-model", "instructions": "Updated purpose", "tools": tools},
        }}}
        with tempfile.TemporaryDirectory() as output:
            with patch.object(sys, "argv", ["fetch_target_agent.py", "--agent", "target",
                                           "--endpoint", "https://project.example", "--out-dir", output]), \
                    patch.object(fetch_target_agent, "_token", return_value="test-token"), \
                    patch.object(fetch_target_agent, "_fetch_agent", return_value=agent), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(fetch_target_agent.main(), 0)
            target = yaml.safe_load((Path(output) / "target-agent.yaml").read_text(encoding="utf-8"))
            inventory = json.loads((Path(output) / "target-agent.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(target["model"], "new-model")
        self.assertEqual(target["instructions"], "Updated purpose")
        self.assertEqual(target["tools"], tools)
        self.assertEqual(inventory["version"], "11")
        self.assertEqual(set(inventory["mcp_inventory"]), {"sql", "docs"})
        self.assertTrue(all(entry["skills"] for entry in inventory["mcp_inventory"].values()))


class RawAttachmentPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_upload_bytes_and_conversation_isolation_preserved(self):
        upload = types.SimpleNamespace(attachments=[types.SimpleNamespace(
            name="hosted_agent_ppt_template 4.pptx",
            content_type="application/vnd.microsoft.teams.file.download.info",
            content={"downloadUrl": "https://download.example/template"},
        )])
        raw_template = b"original-template-bytes"
        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=raw_template))
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(shared_file_store, "_FILES_DIR", Path(directory)), \
                patch.object(httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=transport, **kwargs)):
            uploaded = await shared_file_store.download_shared_files(upload, "conversation-one")
            self.assertEqual(len(uploaded), 1)
            saved = Path(uploaded[0]["path"])
            self.assertEqual(saved.read_bytes(), raw_template)
            self.assertEqual(saved.name, "hosted_agent_ppt_template_4.pptx")
            self.assertEqual(saved.parent, shared_file_store._conv_dir("conversation-one"))
            self.assertNotEqual(saved.parent, shared_file_store._conv_dir("conversation-two"))

    async def test_original_file_attachment_is_passed_to_sdk_without_prompt_injection(self):
        event_types = types.SimpleNamespace(**{name: name for name in (
            "TOOL_EXECUTION_START", "ASSISTANT_MESSAGE_DELTA", "ASSISTANT_MESSAGE",
            "SESSION_IDLE", "ASSISTANT_IDLE", "SESSION_ERROR",
        )})

        class Session:
            def on(self, handler):
                self.handler = handler
                return lambda: None

            async def send(self, text, attachments=None):
                self.sent_text = text
                self.sent_attachments = attachments
                self.handler(types.SimpleNamespace(type=event_types.SESSION_IDLE, data=types.SimpleNamespace()))

        copilot_stub = types.SimpleNamespace(
            CopilotClient=object, PermissionHandler=types.SimpleNamespace(approve_all=None),
            ProviderConfig=object, SessionEventType=event_types, ToolSet=object,
        )
        spec = importlib.util.spec_from_file_location(
            "client_attachment_test", Path(__file__).parent / "src" / "activity-agent" / "client.py",
        )
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"copilot": copilot_stub, "tools": types.SimpleNamespace(set_call_id=lambda *_args: None)}):
            spec.loader.exec_module(module)
        session = Session()
        with tempfile.TemporaryDirectory() as directory, patch.object(module, "_get_session", new=AsyncMock(return_value=session)):
            template = Path(directory) / "template.pptx"
            template.write_bytes(b"raw-template")
            uploaded = [{"name": "template.pptx", "path": str(template.resolve()), "kind": "file"}]
            async for _result in module.ask_stream("conversation", "Use the template", uploaded,
                                                   auth_user_id="attachment-owner", call_id="attachment-call"):
                pass
            self.assertEqual(session.sent_text, "Use the template")
            self.assertEqual(session.sent_attachments, [
                {"type": "file", "path": str(template.resolve()), "displayName": "template.pptx"},
            ])


class AuthRetryStateTests(unittest.TestCase):
    def setUp(self):
        storage = tempfile.TemporaryDirectory()
        self.addCleanup(storage.cleanup)
        for active_patch in (patch.object(auth_retry, "_STATE_DIR", Path(storage.name)),
                             patch.object(auth_retry, "_ACTIVE", {})):
            active_patch.start()
            self.addCleanup(active_patch.stop)
        auth_retry.begin_turn("conversation", "user-one", "Run my query", "session-one")
        self.operation = {"kind": "call", "toolbox": "docs", "name": "search_documents", "arguments": {"text": "report"}}

    def test_pending_operation_is_one_shot_and_preserves_original_request(self):
        retry_id = auth_retry.stage("conversation", self.operation)
        self.assertTrue(retry_id)
        self.assertEqual(auth_retry.stage("conversation", self.operation), retry_id)
        pending = auth_retry.take("conversation", "user-one", "session-one", retry_id)
        self.assertEqual(pending["operation"], self.operation)
        self.assertEqual(pending["request"], "Run my query")
        self.assertIsNone(auth_retry.take("conversation", "user-one", "session-one", retry_id))

    def test_other_user_or_conversation_cannot_take_retry(self):
        retry_id = auth_retry.stage("conversation", self.operation)
        self.assertIsNone(auth_retry.take("conversation", "user-two", "session-one", retry_id))
        self.assertIsNone(auth_retry.take("different-conversation", "user-one", "session-one", retry_id))
        self.assertIsNotNone(auth_retry.take("conversation", "user-one", "session-one", retry_id))

    def test_transient_claim_read_failure_restores_pending_operation(self):
        retry_id = auth_retry.stage("conversation", self.operation)
        original_read = Path.read_text

        def fail_claim_read(path, *args, **kwargs):
            if path.suffix == ".claimed":
                raise OSError("Simulated transient read failure")
            return original_read(path, *args, **kwargs)

        with patch.object(Path, "read_text", autospec=True, side_effect=fail_claim_read):
            self.assertIsNone(auth_retry.take("conversation", "user-one", "session-one", retry_id))
        self.assertEqual(auth_retry.peek("conversation", "user-one", "session-one")["id"], retry_id)
        self.assertEqual(list(auth_retry._STATE_DIR.rglob("*.claimed")), [])
        self.assertIsNotNone(auth_retry.take("conversation", "user-one", "session-one", retry_id))
        self.assertIsNone(auth_retry.take("conversation", "user-one", "session-one", retry_id))
        self.assertEqual(list(auth_retry._STATE_DIR.rglob("*.claimed")), [])

    def test_stale_button_does_not_consume_new_pending_request(self):
        previous = auth_retry.stage("conversation", self.operation)
        self.assertIsNotNone(auth_retry.take("conversation", "user-one", "session-one", previous))
        current = auth_retry.stage("conversation", {**self.operation, "name": "new-tool"})
        self.assertNotEqual(previous, current)
        self.assertIsNone(auth_retry.take("conversation", "user-one", "session-one", previous))
        self.assertIsNotNone(auth_retry.take("conversation", "user-one", "session-one", current))

    def test_failed_claim_restore_preserves_claim_for_recovery(self):
        retry_id = auth_retry.stage("conversation", self.operation)
        original_read = Path.read_text

        def fail_claim_read(path, *args, **kwargs):
            if path.suffix == ".claimed":
                raise OSError("Simulated transient read failure")
            return original_read(path, *args, **kwargs)

        with patch.object(Path, "read_text", autospec=True, side_effect=fail_claim_read), \
                patch.object(auth_retry.os, "link", side_effect=OSError("Simulated restore failure")):
            self.assertIsNone(auth_retry.take("conversation", "user-one", "session-one", retry_id))
        claims = list(auth_retry._STATE_DIR.rglob("*.claimed"))
        self.assertEqual(len(claims), 1)
        self.assertEqual(json.loads(claims[0].read_text(encoding="utf-8"))["id"], retry_id)
        self.assertIsNone(auth_retry.take("conversation", "user-one", "session-one", retry_id))

    def test_claim_restore_does_not_overwrite_replacement_entry(self):
        retry_id = auth_retry.stage("conversation", self.operation)
        path, data = auth_retry._pending("conversation", "user-one", "session-one")[0]
        replacement = {**data, "id": "replacement", "operation": {**self.operation, "name": "replacement-tool"}}
        original_read = Path.read_text

        def replace_before_failed_read(claimed, *args, **kwargs):
            if claimed.suffix == ".claimed":
                path.write_text(json.dumps(replacement), encoding="utf-8")
                raise OSError("Simulated concurrent replacement")
            return original_read(claimed, *args, **kwargs)

        with patch.object(Path, "read_text", autospec=True, side_effect=replace_before_failed_read):
            self.assertIsNone(auth_retry.take("conversation", "user-one", "session-one", retry_id))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), replacement)
        self.assertEqual(len(list(auth_retry._STATE_DIR.rglob("*.claimed"))), 1)

    def test_successful_claim_cleanup_error_does_not_replay_operation(self):
        retry_id = auth_retry.stage("conversation", self.operation)
        original_unlink = Path.unlink

        def fail_claim_cleanup(path, *args, **kwargs):
            if path.suffix == ".claimed":
                raise OSError("Simulated cleanup failure")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", autospec=True, side_effect=fail_claim_cleanup):
            self.assertIsNotNone(auth_retry.take("conversation", "user-one", "session-one", retry_id))
        self.assertIsNone(auth_retry.take("conversation", "user-one", "session-one", retry_id))
        self.assertEqual(len(list(auth_retry._STATE_DIR.rglob("*.claimed"))), 1)

    def test_multiple_pending_operations_are_independent_for_any_connector_count(self):
        for count in (1, 3, 7):
            with self.subTest(connectors=count):
                operations = [{**self.operation, "toolbox": f"connector-{index}"} for index in range(count)]
                retry_ids = [auth_retry.stage("conversation", operation) for operation in operations]
                self.assertEqual(len(set(retry_ids)), count)
                for retry_id, operation in reversed(list(zip(retry_ids, operations))):
                    self.assertEqual(auth_retry.stage("conversation", operation), retry_id)
                    claimed = auth_retry.take("conversation", "user-one", "session-one", retry_id)
                    self.assertEqual(claimed["operation"], operation)
                    self.assertIsNone(auth_retry.take("conversation", "user-one", "session-one", retry_id))
                self.assertIsNone(auth_retry.peek("conversation", "user-one", "session-one"))

    def test_next_message_takes_oldest_without_dropping_other_operations(self):
        with patch.object(auth_retry.time, "time", return_value=100):
            first = auth_retry.stage("conversation", self.operation)
        with patch.object(auth_retry.time, "time", return_value=101):
            second = auth_retry.stage("conversation", {**self.operation, "toolbox": "another-connector"})
            self.assertEqual(auth_retry.take("conversation", "user-one", "session-one")["id"], first)
            self.assertEqual(auth_retry.peek("conversation", "user-one", "session-one")["id"], second)

    def test_existing_single_pending_file_remains_compatible(self):
        retry_id = auth_retry.stage("conversation", self.operation)
        path, _data = auth_retry._pending("conversation", "user-one", "session-one")[0]
        path.rename(auth_retry._path("conversation", "user-one"))
        self.assertEqual(auth_retry.stage("conversation", self.operation), retry_id)
        self.assertIsNotNone(auth_retry.take("conversation", "user-one", "session-one", retry_id))

    def test_expired_or_reset_session_request_is_not_replayed(self):
        with patch.object(auth_retry.time, "time", return_value=100):
            retry_id = auth_retry.stage("conversation", self.operation)
        with patch.object(auth_retry.time, "time", return_value=1301):
            self.assertIsNone(auth_retry.take("conversation", "user-one", "session-one", retry_id))
        retry_id = auth_retry.stage("conversation", self.operation)
        self.assertIsNone(auth_retry.take("conversation", "user-one", "session-two", retry_id))

    def test_credentials_and_missing_user_are_not_persisted(self):
        operation = {**self.operation, "arguments": {"client_secret": "never-save-this"}}
        self.assertIsNone(auth_retry.stage("conversation", operation))
        auth_retry.begin_turn("conversation", "", "Run my query", "session-one")
        self.assertIsNone(auth_retry.stage("conversation", self.operation))
        self.assertEqual(list(auth_retry._STATE_DIR.rglob("*.json")), [])

    def test_pending_state_survives_active_context_clear_and_can_be_cancelled(self):
        auth_retry.stage("conversation", self.operation)
        auth_retry.end_turn("conversation")
        self.assertIsNotNone(auth_retry.peek("conversation", "user-one", "session-one"))
        auth_retry.clear_conversation("conversation")
        self.assertIsNone(auth_retry.peek("conversation", "user-one", "session-one"))


class AuthReplayFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        storage = tempfile.TemporaryDirectory()
        self.addCleanup(storage.cleanup)
        for active_patch in (patch.object(auth_retry, "_STATE_DIR", Path(storage.name) / "pending"),
                             patch.object(auth_retry, "_ACTIVE", {})):
            active_patch.start()
            self.addCleanup(active_patch.stop)
        root = Path(__file__).parent / "src" / "activity-agent"
        tools_spec = importlib.util.spec_from_file_location("tools_auth_flow_test", root / "tools.py")
        self.tools = importlib.util.module_from_spec(tools_spec)
        with patch.dict(sys.modules, {
            "copilot": types.SimpleNamespace(Tool=object, define_tool=lambda *_args, **_kwargs: None),
            "pydantic": types.SimpleNamespace(BaseModel=object, Field=lambda **_kwargs: None),
        }):
            tools_spec.loader.exec_module(self.tools)
        self.event_types = types.SimpleNamespace(**{name: name for name in (
            "TOOL_EXECUTION_START", "ASSISTANT_MESSAGE_DELTA", "ASSISTANT_MESSAGE",
            "SESSION_IDLE", "ASSISTANT_IDLE", "SESSION_ERROR",
        )})
        event_types = self.event_types

        class Session:
            def on(self, handler):
                self.handler = handler
                return lambda: None

            async def send(self, text, attachments=None):
                self.text = text
                self.handler(types.SimpleNamespace(type=event_types.ASSISTANT_MESSAGE,
                                                  data=types.SimpleNamespace(content="Recovered tool result")))
                self.handler(types.SimpleNamespace(type=event_types.SESSION_IDLE, data=types.SimpleNamespace()))

        self.session = Session()
        client_spec = importlib.util.spec_from_file_location("client_auth_flow_test", root / "client.py")
        self.client = importlib.util.module_from_spec(client_spec)
        with patch.dict(sys.modules, {
            "copilot": types.SimpleNamespace(CopilotClient=object, PermissionHandler=object,
                                             ProviderConfig=object, SessionEventType=event_types, ToolSet=object),
            "tools": self.tools,
        }):
            client_spec.loader.exec_module(self.client)
        self.client._SESSION_STATE_DIR = Path(storage.name) / "sessions"
        active_patch = patch.object(self.client, "_get_session", new=AsyncMock(return_value=self.session))
        active_patch.start()
        self.addCleanup(active_patch.stop)
        self.operation = {"kind": "call", "toolbox": "docs", "name": "search_documents", "arguments": {"text": "report"}}
        auth_retry.begin_turn("conversation", "owner", "Find my original report", "")
        self.retry_id = auth_retry.stage("conversation", self.operation)
        auth_retry.end_turn("conversation")

    async def collect(self, text="done", user="owner", retry_id=None):
        return [item async for item in self.client.ask_stream(
            "conversation", text, auth_user_id=user, call_id="fresh-request-call", retry_id=retry_id,
        )]

    async def test_next_message_retries_exact_operation_with_fresh_context(self):
        with patch.object(toolbox, "toolbox_request", return_value={"status": "ok", "result": {"text": "report"}}) as invoke:
            result = await self.collect("done")
        invoke.assert_called_once_with("docs", name="search_documents", arguments={"text": "report"}, call_id="fresh-request-call")
        saved = json.loads(self.session.text.rsplit("\n", 1)[1])
        self.assertEqual(saved["original_request"], "Find my original report")
        self.assertEqual(saved["retried_operation"], self.operation)
        self.assertEqual(saved["new_user_message"], "done")
        self.assertEqual(result[-1], ("final", "Recovered tool result"))
        self.assertIsNone(auth_retry.peek("conversation", "owner", ""))

    async def test_button_replay_is_one_shot(self):
        with patch.object(toolbox, "toolbox_request", return_value={"status": "ok", "result": {}}) as invoke:
            await self.collect("", retry_id=self.retry_id)
            repeated = await self.collect("", retry_id=self.retry_id)
        self.assertEqual(invoke.call_count, 1)
        self.assertIn("already been retried", repeated[-1][1])

    async def test_wrong_user_cannot_replay_pending_operation(self):
        with patch.object(toolbox, "toolbox_request") as invoke:
            result = await self.collect("", user="other-user", retry_id=self.retry_id)
        invoke.assert_not_called()
        self.assertIn("different session", result[-1][1])
        self.assertIsNotNone(auth_retry.peek("conversation", "owner", ""))

    async def test_still_unauthorized_queues_new_card_without_looping(self):
        with patch.object(toolbox, "toolbox_request", return_value={"status": "consent", "url": "https://consent.example/login"}) as invoke:
            result = await self.collect("done")
        self.assertEqual(invoke.call_count, 1)
        self.client._get_session.assert_not_awaited()
        self.assertIn("Sign-in is still required", result[-1][1])
        actions = self.tools.drain_ui("conversation")
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["type"], "consent")
        self.assertNotEqual(actions[0]["retry_id"], self.retry_id)

    async def test_targeted_retry_preserves_other_connectors_pending_operation(self):
        second = {**self.operation, "toolbox": "another-connector"}
        auth_retry.begin_turn("conversation", "owner", "Find my original report", "")
        second_id = auth_retry.stage("conversation", second)
        auth_retry.end_turn("conversation")
        with patch.object(toolbox, "toolbox_request", return_value={"status": "ok", "result": {}}) as invoke:
            await self.collect("", retry_id=second_id)
            self.assertEqual(invoke.call_args.args[0], "another-connector")
            saved = json.loads(self.session.text.rsplit("\n", 1)[1])
            self.assertTrue(saved["more_pending_operations"])
            self.assertEqual(auth_retry.peek("conversation", "owner", "")["id"], self.retry_id)
            await self.collect("done")
            self.assertEqual(invoke.call_args.args[0], "docs")
        self.assertEqual(invoke.call_count, 2)
        self.assertIsNone(auth_retry.peek("conversation", "owner", ""))

    async def test_successive_consent_challenges_have_no_fixed_signin_count(self):
        for count in (1, 3, 5):
            with self.subTest(challenges=count):
                self.client._get_session.reset_mock()
                auth_retry.begin_turn("conversation", "owner", "Find my original report", "")
                retry_id = auth_retry.stage("conversation", self.operation)
                auth_retry.end_turn("conversation")
                challenges = [{"status": "consent", "source": f"connector-{index}",
                               "url": f"https://consent-{index}.example/login"} for index in range(count)]
                with patch.object(toolbox, "toolbox_request", side_effect=[*challenges, {"status": "ok", "result": {}}]) as invoke:
                    for index in range(count):
                        result = await self.collect("", retry_id=retry_id)
                        self.assertIn("Sign-in is still required", result[-1][1])
                        self.client._get_session.assert_not_awaited()
                        actions = self.tools.drain_ui("conversation")
                        self.assertEqual(len(actions), 1)
                        self.assertEqual(actions[0]["label"], f"connector-{index}")
                        retry_id = actions[0]["retry_id"]
                        self.assertEqual(auth_retry.peek("conversation", "owner", "")["request"], "Find my original report")
                    result = await self.collect("", retry_id=retry_id)
                self.assertEqual(invoke.call_count, count + 1)
                self.assertEqual(result[-1], ("final", "Recovered tool result"))
                self.client._get_session.assert_awaited_once()
                self.assertIsNone(auth_retry.peek("conversation", "owner", ""))


class ConsentCardAndHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.activity_sdk = types.ModuleType("microsoft_agents.activity")
        self.activity_sdk.Attachment = lambda **fields: types.SimpleNamespace(**fields)
        self.activity_sdk.Activity = lambda **fields: types.SimpleNamespace(**fields)
        self.activity_sdk.InvokeResponse = lambda **fields: types.SimpleNamespace(**fields)
        self.activity_sdk.ActivityTypes = types.SimpleNamespace(message="message", invoke_response="invokeResponse")
        active_patch = patch.dict(sys.modules, {"microsoft_agents.activity": self.activity_sdk})
        active_patch.start()
        self.addCleanup(active_patch.stop)
        root = Path(__file__).parent / "src" / "activity-agent"
        spec = importlib.util.spec_from_file_location("cards_auth_test", root / "cards.py")
        self.cards = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cards)
        self.calls = []

        async def ask_stream(conversation, text, *args, **kwargs):
            self.calls.append((conversation, text, kwargs))
            yield "final", "Retried original request"

        self.namespace = {
            "__name__": "auth_handler_tests", "asyncio": asyncio, "logger": toolbox.logger,
            "cards": self.cards, "copilot_client": types.SimpleNamespace(
                ask_stream=ask_stream, check_session_expiry=lambda _id: (False, ""),
            ),
            "outfiles": types.SimpleNamespace(is_file_consent_invoke=lambda _activity: False),
            "files": types.SimpleNamespace(download_shared_files=AsyncMock(return_value=[])),
            "_tools": types.SimpleNamespace(set_call_id=lambda *_args: None),
            "_AUTH_RETRY_TASKS": set(), "_current_call_id": lambda: "fresh-handler-call",
            "_deliver_ui": AsyncMock(), "_log_message_content": lambda *_args: None,
            "environ": {}, "_RELEASE_LABEL": "test",
        }
        parsed = ast.parse((root / "main.py").read_text(encoding="utf-8"))
        selected = [node for node in parsed.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name in ("_activity_user_id", "_run_auth_retry", "on_invoke", "on_message")]
        for node in selected:
            node.decorator_list = []
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(root / "main.py"), "exec"), self.namespace)

    def context(self, text="", value=None):
        return types.SimpleNamespace(
            activity=types.SimpleNamespace(text=text, name=self.cards.ADAPTIVE_ACTION_INVOKE,
                                           conversation=types.SimpleNamespace(id="conversation"),
                                           from_property=types.SimpleNamespace(aad_object_id="owner"), value=value),
            send_activity=AsyncMock(), turn_state={},
        )

    def test_card_has_signin_and_opaque_message_retry(self):
        card = self.cards.build_consent_card("https://consent.example/login", "source", "opaque-id")
        self.assertEqual(card["actions"][0]["type"], "Action.OpenUrl")
        retry = card["actions"][1]
        self.assertEqual(retry["type"], "Action.Submit")
        self.assertEqual(retry["data"], {"authRetryId": "opaque-id"})
        self.assertNotIn("original_request", json.dumps(card))

    async def test_old_retry_invoke_returns_message_button_without_detached_execution(self):
        context = self.context(value={"action": {"verb": "retry_auth", "data": {"retryId": "opaque-id"}}})
        with patch.object(asyncio, "create_task", side_effect=AssertionError("Retry work must not outlive the invoke request.")):
            await self.namespace["on_invoke"](context, None)
        self.assertEqual(context.send_activity.await_count, 1)
        ack = context.send_activity.await_args.args[0]
        self.assertEqual(ack.type, "invokeResponse")
        self.assertEqual(ack.value.status, 200)
        self.assertEqual(ack.value.body["value"]["$schema"], "http://adaptivecards.io/schemas/adaptive-card.json")
        retry = ack.value.body["value"]["actions"][0]
        self.assertEqual(retry["type"], "Action.Submit")
        self.assertEqual(retry["data"], {"authRetryId": "opaque-id"})
        self.assertEqual(self.calls, [])
        self.assertFalse(self.namespace["_AUTH_RETRY_TASKS"])

    async def test_unavailable_legacy_retry_card_has_schema_and_no_action(self):
        context = self.context(value={"action": {"verb": "retry_auth", "data": {}}})
        await self.namespace["on_invoke"](context, None)
        card = context.send_activity.await_args.args[0].value.body["value"]
        self.assertEqual(card["$schema"], "http://adaptivecards.io/schemas/adaptive-card.json")
        self.assertEqual(card["actions"], [])
        self.assertEqual(self.calls, [])

    async def test_next_message_passes_fresh_user_context_without_changing_text(self):
        context = self.context(text="done")
        await self.namespace["on_message"](context, None)
        self.assertEqual(self.calls[0][1], "done")
        self.assertEqual(self.calls[0][2], {"auth_user_id": "owner", "call_id": "fresh-handler-call", "retry_id": None})

    async def test_submit_fallback_passes_retry_id_to_same_message_path(self):
        context = self.context(value={"authRetryId": "opaque-id"})
        await self.namespace["on_message"](context, None)
        self.assertEqual(self.calls[0][2]["retry_id"], "opaque-id")


class SessionLifecycleSmokeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.storage = tempfile.TemporaryDirectory()
        self.addCleanup(self.storage.cleanup)
        self.resumed_ids = []

        class FakeToolSet:
            def add_builtin(self, _name):
                return self

            def add_custom(self, _name):
                return self

        resumed_ids = self.resumed_ids

        class FakeClient:
            async def start(self):
                pass

            async def resume_session(self, session_id, **_options):
                resumed_ids.append(session_id)
                return types.SimpleNamespace(abort=AsyncMock())

        copilot_stub = types.SimpleNamespace(
            CopilotClient=FakeClient, PermissionHandler=types.SimpleNamespace(approve_all=None),
            ProviderConfig=lambda **options: options, SessionEventType=types.SimpleNamespace(), ToolSet=FakeToolSet,
        )
        spec = importlib.util.spec_from_file_location(
            "client_lifecycle_test", Path(__file__).parent / "src" / "activity-agent" / "client.py",
        )
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"copilot": copilot_stub, "tools": types.SimpleNamespace(build_tools=lambda _id: [])}):
            spec.loader.exec_module(self.module)
        self.module._SESSION_STATE_DIR = Path(self.storage.name)
        self.module._ENDPOINT = "https://project.example"
        self.module._EFFECTIVE_MODEL = "model"
        for active_patch in (
            patch.object(self.module, "_fresh_token", return_value="test-token"),
            patch.object(self.module, "_system_message_with_skills", return_value="skill-index"),
        ):
            active_patch.start()
            self.addCleanup(active_patch.stop)

    async def test_sdk_id_is_stable_short_and_changes_after_new(self):
        conversation = "conversation-" + "long-id" * 100
        initial = self.module._sdk_session_id(conversation)
        self.assertEqual(self.module._sdk_session_id(conversation), initial)
        self.assertLessEqual(len(initial), 64)
        await self.module.reset_session(conversation)
        self.assertNotEqual(self.module._sdk_session_id(conversation), initial)

    async def test_reset_aborts_only_own_cached_session_and_resets_counters(self):
        first = await self.module._get_session("one")
        other = await self.module._get_session("two")
        self.module._get_meta("one").turn_count = 3
        initial_id = self.module._sdk_session_id("one")
        await self.module.reset_session("one")
        first.abort.assert_awaited_once()
        other.abort.assert_not_awaited()
        self.assertNotIn("one", self.module._sessions)
        self.assertIs(self.module._sessions["two"][0], other)
        self.assertEqual(self.module.session_status("one")["turns"], 0)
        await self.module._get_session("one")
        self.assertNotEqual(self.resumed_ids[-1], initial_id)

    async def test_nonce_and_status_survive_metadata_cache_reload(self):
        await self.module.reset_session("conversation")
        expected_id = self.module._sdk_session_id("conversation")
        meta = self.module._get_meta("conversation")
        meta.turn_count = 2
        self.module._save_meta("conversation", meta)
        self.module._session_meta.clear()
        self.assertEqual(self.module._sdk_session_id("conversation"), expected_id)
        self.assertEqual(self.module.session_status("conversation")["turns"], 2)

    async def test_idle_reminder_does_not_silently_change_session(self):
        self.module.SESSION_TIMEOUT_S = 60
        meta = self.module._get_meta("conversation")
        meta.last_activity = 100
        original_id = self.module._sdk_session_id("conversation")
        with patch.object(self.module.time, "time", return_value=159):
            self.assertFalse(self.module.check_session_expiry("conversation")[0])
        with patch.object(self.module.time, "time", return_value=160):
            expired, reason = self.module.check_session_expiry("conversation")
            self.assertTrue(expired)
            self.assertIn("last message", reason)
        self.assertEqual(self.module._sdk_session_id("conversation"), original_id)

    async def test_invalid_json_metadata_falls_back_to_new_session(self):
        path = self.module._SESSION_STATE_DIR / "conversation.json"
        path.write_text("{broken", encoding="utf-8")
        self.assertFalse(self.module.session_status("conversation")["active"])

    async def test_non_mapping_metadata_does_not_crash_status(self):
        path = self.module._SESSION_STATE_DIR / "conversation.json"
        path.write_text("[]", encoding="utf-8")
        self.assertFalse(self.module.session_status("conversation")["active"])

    async def test_reset_during_session_open_cannot_cache_old_session_id(self):
        opening = asyncio.Event()
        release = asyncio.Event()
        resumed = []

        class BlockingClient:
            async def resume_session(self, session_id, **_options):
                resumed.append(session_id)
                if len(resumed) == 1:
                    opening.set()
                    await release.wait()
                return types.SimpleNamespace(abort=AsyncMock())

        self.module._client = BlockingClient()
        original_id = self.module._sdk_session_id("conversation")

        async def open_in_turn():
            async with self.module._turn_lock:
                return await self.module._get_session("conversation")

        opening_task = asyncio.create_task(open_in_turn())
        await asyncio.wait_for(opening.wait(), timeout=1)
        reset_task = asyncio.create_task(self.module.reset_session("conversation"))
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(opening_task, reset_task), timeout=1)
        self.assertNotEqual(self.module._sdk_session_id("conversation"), original_id)
        await self.module._get_session("conversation")
        self.assertNotEqual(resumed[-1], original_id)


class SkillSessionRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_changed_skill_index_updates_same_conversation_session(self):
        class FakeToolSet:
            def add_builtin(self, _name):
                return self

            def add_custom(self, _name):
                return self

        class FakeClient:
            def __init__(self):
                self.calls = []

            async def start(self):
                pass

            async def resume_session(self, session_id, **options):
                self.calls.append((session_id, options))
                return object()

        copilot_stub = types.SimpleNamespace(
            CopilotClient=FakeClient, PermissionHandler=types.SimpleNamespace(approve_all=None),
            ProviderConfig=lambda **options: options, SessionEventType=types.SimpleNamespace(), ToolSet=FakeToolSet,
        )
        spec = importlib.util.spec_from_file_location(
            "client_refresh_test", Path(__file__).parent / "src" / "activity-agent" / "client.py",
        )
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"copilot": copilot_stub, "tools": types.SimpleNamespace(build_tools=lambda _id: [])}):
            spec.loader.exec_module(module)
        with patch.object(module, "_ENDPOINT", "https://project.example"), \
                patch.object(module, "_EFFECTIVE_MODEL", "model"), \
                patch.object(module, "_fresh_token", return_value="test-token"), \
                patch.object(module, "_system_message_with_skills", return_value="skill-index-one") as skill_index:
            first = await module._get_session("conversation")
            self.assertIs(await module._get_session("conversation"), first)
            self.assertEqual(len(module._client.calls), 1)
            skill_index.return_value = "skill-index-two"
            second = await module._get_session("conversation")
            self.assertIsNot(first, second)
            self.assertEqual(len(module._client.calls), 2)
            self.assertEqual(module._client.calls[0][0], module._client.calls[1][0])
            self.assertEqual(module._client.calls[1][1]["system_message"]["content"], "skill-index-two")


if __name__ == "__main__":
    unittest.main()