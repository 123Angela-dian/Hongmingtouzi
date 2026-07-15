# Asset Radar LangSmith Workflow

This is the backend workflow scaffold for the Asset Radar.

It is designed to run in two modes:

- Local dry run: no LangSmith package or API key required.
- LangSmith tracing: install `requirements.txt` and set LangSmith environment variables.

## Quick Start

```powershell
cd C:\Users\HP\Documents\90天计划\asset-radar-langsmith
C:\Users\HP\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m src.asset_radar.run_demo
```

## LangSmith Environment

```powershell
$env:LANGSMITH_TRACING="true"
$env:LANGSMITH_PROJECT="asset radar"
$env:LANGSMITH_PROJECT_ID="9d5f165c-bef9-4bb8-882f-e3da1ea31491"
$env:LANGSMITH_API_KEY="..."
```

Optional:

```powershell
pip install -r requirements.txt
```

LangSmith project URL:

```text
https://smith.langchain.com/o/1603f6ae-a220-409d-b48d-c28f1817f39a/projects/p/9d5f165c-bef9-4bb8-882f-e3da1ea31491
```

## Workflow Nodes

1. `source_scan_node`
2. `coarse_filter_node`
3. `dedupe_node`
4. `detailed_screening_node`
5. `asset_pool_update_node`
6. `interest_alert_node`

The current MVP intentionally does not filter by amount.

## Node Model Routing

The workflow routes nodes to CherryIN models as follows:

| Node | Model |
|---|---|
| `source_scan_node` | `deepseek/deepseek-v4-pro` |
| `coarse_filter_node` | `deepseek/deepseek-v4-pro` |
| `dedupe_node` simple case | `deepseek/deepseek-v4-pro` |
| `dedupe_node` complex case | `z-ai/glm-5.2` |
| `detailed_screening_node` | `anthropic/claude-opus-4.8` |
| `asset_pool_update_node` | `openai/gpt-4o-mini` |
| `interest_alert_node` | `deepseek/deepseek-v4-pro` |
| report generation / deep analysis | `anthropic/claude-opus-4.8` |

Runtime secrets are read from environment variables and are not stored in this
repository:

```powershell
$env:CHERRYIN_API_KEY="..."
$env:CHERRYIN_BASE_URL="https://open.cherryin.net/v1"
```

Connectivity test:

```powershell
C:\Users\HP\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m src.asset_radar.test_cherryin
```

## Interest Alert Demo

```powershell
C:\Users\HP\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m src.asset_radar.run_interest_demo
```

This seeds one interested asset, simulates a second auction / price drop notice,
updates the existing asset record, and emits an alert.
