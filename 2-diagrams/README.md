# 2 — Diagrams

> One-line summary per diagram. Excalidraw files open at
> [excalidraw.com](https://excalidraw.com) (or the VS Code Excalidraw extension);
> `.html` files open in a browser; `.png` are static exports.

> **Curation note:** this folder is a working set — prune/rename as you like and
> update this list to match. A few files still contain sample names/hosts.

## Start here
| Diagram | What it shows |
|---|---|
| `private_foundry_to_teams_architecture.excalidraw` | The whole picture: APIM + shared container + private Foundry. **Start here.** |
| `private_foundry_to_teams_architecture_v2.excalidraw` | Updated v2 of the end-to-end architecture. |
| `traffic_flow_teams_to_foundry.excalidraw` | A single Teams message's private path to the agent and back. |
| `traffic_flow_teams_to_foundry.html` | Browser render of the same traffic flow. |
| `boundary_a_b_infographic.excalidraw` | The two auth boundaries (A: identity → MCP, B: MCP → data as user) and where consent/OBO happen. |

## Publishing approaches (REST API vs custom translator)
| Diagram | What it shows |
|---|---|
| `native_path_architecture.excalidraw` | The native-channel architecture and its per-user-tool limitation. |
| `shared_container_router_explained.excalidraw` | Why one shared router container serves all agents. |
| `streamlit_publisher_flow.excalidraw` | What "publish" does under the hood via the Streamlit app. |
| `streamlit_publisher_flow_architecture_style.html` | The publisher flow, architecture-diagram style. |
| `streamlit_publisher_flow_symbols.html` | The publisher flow with a legend / symbols. |
| `publishing_approaches_at_a_glance.html` | REST API vs custom translator, at a glance. |
| `publishing_approaches_high_level.html` | High-level publishing-approach comparison. |
| `PUBLISHING_FLOW_DIAGRAM_SCRIPT.md` | Notes/script behind the publishing-flow diagrams. |

## Function-App (per-agent) path — for comparison
| Diagram | What it shows |
|---|---|
| `function_app_flow_no_apim_teams_to_foundry.excalidraw` | Per-agent Function App path to Foundry without APIM. |
| `gang_function_app_flow_teams_to_foundry.excalidraw` | Per-agent Function App flow (working-session variant). *Rename/remove during curation.* |

## Infrastructure build (layered)
| Diagram | What it shows |
|---|---|
| `layer1_network_foundation.png` | Layer 1 — VNet / network foundation. |
| `layer2_data_resources.png` | Layer 2 — data resources (storage, Cosmos, AI Search). |
| `layer3_ai_services.png` | Layer 3 — the Foundry AI Services account. |
| `layer4_project_connections.png` | Layer 4 — project + connections. |
| `layer5_capability_host.png` | Layer 5 — the capability host. |
| `deployment_flow.png` | High-level order of the deployment steps. |
| `repo_build_steps_chronological.excalidraw` | Chronological build steps of the solution. |
| `cisco_nva_edge_firewall.excalidraw` | Optional edge firewall / NVA topology at the perimeter. |

## `rest_api_custom_translator_comparison/` (deck & test set)
| File | What it shows |
|---|---|
| `bot_service_communication_native_vs_custom.html` | Bot Service comms: native channel vs custom translator. |
| `rest_api_publish_flow_private_foundry.html` | REST-API publish flow for private Foundry. |
| `rest_api_test_validation_flow.html` | Validation flow for the REST-API test. |
| `rest_api_test_summary.html` / `REST_API_TEST_SUMMARY.md` | Summary of the REST-API native-channel test. |
| `messaging_endpoint_decoded.html` | Anatomy of the bot messaging-endpoint URL. *Contains a sample host — generalize.* |
| `messaging_endpoint_decoded_generalized.html` | Generalized version of the messaging-endpoint breakdown. |
| `publishing_approaches_at_a_glance.html` | At-a-glance comparison (deck copy). |
| `publishing_approaches_high_level.html` | High-level comparison (deck copy). |
| `publishing_approaches_client_deck.html` | Client-deck version of the comparison. |
| `streamlit_publisher_flow_shared_container.html` | Publisher flow via the shared container. |
| `traffic_flow_teams_to_foundry.html` | Traffic flow (comparison-set copy). |
| `PUBLISHING_FLOW_DIAGRAM_SCRIPT.md` | Script/notes for the comparison deck. |
| `start-client-deck.ps1` | Helper to open/serve the client deck locally. |
| `rest_api_test.code-workspace` | VS Code workspace for the REST-API test set. |
