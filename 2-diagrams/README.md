# 2 — Diagrams

> One-line summary per diagram. `.excalidraw` files open at
> [excalidraw.com](https://excalidraw.com) (or the VS Code Excalidraw extension);
> `.html` files open in a browser; `.png` are static exports.

The set is organized top-down: **high-level overview** at the root, then the two
publishing approaches — [`custom-translator-IaC/`](custom-translator-IaC/) (shared
container) and [`native-rest-api/`](native-rest-api/) (Foundry's native channel).

## Start here — high-level overview
| Diagram | What it shows |
|---|---|
| `high_level_architecture.png` | The whole picture at a glance: Teams → Bot → APIM → private Foundry. **Start here.** |
| `private_foundry_to_teams_HIGH_LEVEL_ARCHITECTURE.html` | Interactive browser render of the end-to-end architecture. |
| `publishing_approaches_high_level.html` | The two approaches — REST API native channel vs custom translator — side by side. |

## `custom-translator-IaC/` — shared-container approach
The shared Bot ⇄ Foundry router: architecture, setup order, publish flow, and the
per-message traffic path.

### `architecture/`
| Diagram | What it shows |
|---|---|
| `networking-diagram.excalidraw` | VNet / private-endpoint networking around APIM, the container, and Foundry. |
| `shared_container_router_explained.excalidraw` | Why one shared router container serves every agent (no new compute per agent). |

### `end-to-end-setup-steps/`
| Diagram | What it shows |
|---|---|
| `End_to_End_Setup_Steps_from_scratch.excalidraw` | The full build order, from empty subscription to a published agent. |

### `publish-flow/`
| Diagram | What it shows |
|---|---|
| `streamlit_publisher_flow.excalidraw` | What "publish" does under the hood via the Streamlit app. |
| `streamlit_publisher_flow_v1.html` | Browser render of the publisher flow (v1). |
| `streamlit_publisher_flow_v2.html` | Browser render of the publisher flow (v2, current). |

### `traffic-flow/`
| Diagram | What it shows |
|---|---|
| `traffic_flow_teams_to_foundry.excalidraw` | A single Teams message's private path to the agent and back. |
| `traffic_flow_teams_to_foundry.html` | Browser render of the same traffic flow. |
| `bot_service_communication_native_vs_custom.html` | Bot Service comms compared: native channel vs custom translator. |
| `messaging_endpoint_decoded.html` | Anatomy of the bot messaging-endpoint URL. *Contains a sample host — generalize.* |
| `messaging_endpoint_decoded_generalized.html` | Generalized version of the messaging-endpoint breakdown. |

## `native-rest-api/` — Foundry native-channel approach
| Diagram | What it shows |
|---|---|
| `rest_api_publish_flow_private_foundry.html` | REST-API publish flow for a private Foundry agent (no custom container). |
| `rest_api_test_summary.html` | Summary of the REST-API native-channel test. |

---

*A few files still contain sample names/hosts — generalize before sharing
externally. Prune/rename as you curate and keep this list in sync.*
