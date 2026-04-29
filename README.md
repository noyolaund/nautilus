# 🧪 QA Automation Framework

**AI-powered test automation framework** built for SAP Fiori, SAP WebGUI, and any web platform.  
Uses **Stagehand** (LLM-based element identification) + **Playwright** (browser automation) with **Python**.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                   REST API (FastAPI)                 │
│         POST /execute  ·  POST /execute/async        │
└──────────────────┬──────────────────────────────────┘
                   │ TestSuiteRequest (JSON)
                   ▼
┌─────────────────────────────────────────────────────┐
│              Engine Selector                         │
│    ┌──────────────┐    ┌──────────────────────┐     │
│    │ AI-Native    │    │ Hybrid               │     │
│    │ (Impl. A)    │    │ (Impl. B)            │     │
│    │              │    │                      │     │
│    │ Stagehand    │    │ Playwright → Cache   │     │
│    │ LLM for      │    │ → Stagehand fallback │     │
│    │ every step   │    │                      │     │
│    └──────────────┘    └──────────────────────┘     │
└──────────────────┬──────────────────────────────────┘
                   │ SuiteResult
                   ▼
┌─────────────────────────────────────────────────────┐
│  HTML Report Generator + JSON Structured Logs        │
└─────────────────────────────────────────────────────┘
```

---

## Two Implementations

### Implementation A — Stagehand AI-Native

Every element interaction goes through the LLM. Stagehand "sees" the page and identifies elements by natural language description.

| Aspect | Detail |
|--------|--------|
| **Strategy** | `act()` / `observe()` / `extract()` for every step |
| **Best for** | SAP Fiori (dynamic IDs, Shadow DOM, deep nesting) |
| **Resilience** | Maximum — self-healing when UI changes |
| **Token cost** | Higher (~150-300 tokens per step) |
| **Speed** | Slower (LLM inference per step) |
| **Determinism** | Non-deterministic (LLM interpretation varies) |

### Implementation B — Hybrid Playwright + Stagehand Fallback

Deterministic Playwright selectors first. AI fallback only when selectors fail. Resolved selectors get cached for future runs.

| Aspect | Detail |
|--------|--------|
| **Strategy** | CSS/XPath → Cache → SAP attributes → AI fallback |
| **Best for** | Mixed platforms, CI/CD pipelines |
| **Resilience** | High — degrades gracefully to AI |
| **Token cost** | Low (AI only when needed) |
| **Speed** | Fast when selectors work |
| **Determinism** | Deterministic when selectors are stable |

---

## Project Structure

```
qa-framework/
├── main.py                          # CLI entry point
├── requirements.txt
├── .env.example
│
├── models/
│   └── schemas.py                   # Pydantic models (JSON schema)
│
├── engines/
│   ├── base_engine.py               # Abstract base with retry/logging
│   ├── stagehand_ai_engine.py       # Implementation A
│   └── hybrid_playwright_engine.py  # Implementation B
│
├── api/
│   └── service.py                   # FastAPI REST endpoints
│
├── reports/
│   └── html_report.py               # HTML report generator
│
├── utils/
│   └── logger.py                    # Structured logging + token tracker
│
├── config/
│   └── selector_cache.json          # Auto-generated selector cache (Impl. B)
│
├── tests/
│   └── test_cases/
│       ├── example_sap_fiori.json   # SAP PO creation test
│       └── example_generic_web.json # E-commerce checkout test
│
└── logs/                            # JSON logs + screenshots
```

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your API keys
```

### 3. Run via CLI

```bash
# Execute with Hybrid engine (recommended for mixed platforms)
python main.py run tests/test_cases/example_sap_fiori.json --engine hybrid

# Execute with AI-Native engine (recommended for SAP)
python main.py run tests/test_cases/example_sap_fiori.json --engine ai_native
```

### 4. Run via REST API

```bash
# Start the server
python main.py serve

# Send a test suite (from another terminal)
curl -X POST http://localhost:8000/execute \
  -H "Content-Type: application/json" \
  -d @tests/test_cases/example_sap_fiori.json \
  -G --data-urlencode "engine_type=hybrid"

# Async execution
curl -X POST http://localhost:8000/execute/async \
  -H "Content-Type: application/json" \
  -d @tests/test_cases/example_sap_fiori.json

# Check status
curl http://localhost:8000/status/{run_id}

# Download HTML report
curl http://localhost:8000/report/{run_id} -o report.html
```

### 5. Run via Dashboard (JDE Report Version Workflow)

A web dashboard for the **JDE Report Version Copy** repetitive task. Login once, iterate over many Excel rows, all in the same browser session.

```bash
# Start the dashboard (default port 5000)
python main.py dashboard

# Open in browser
http://localhost:5000
```

**Workflow (4 steps in the UI):**

1. **Start Browser & Login** — Launches Chromium and runs `tests/test_cases/login_assert.json`. Login success is determined by the final `assert_visible "Welcome!"` step.
2. **Load Excel Data** — Pick an `.xlsx` file (with `.xlsx` filter) and a sheet name. The dashboard parses the file, validates that column B (App / Report) starts with `R` or `P`, and shows the valid rows. Rows are classified into three paths based on columns G (left_operand) and I (tab):
   - **Full path** — both G and I have data → `tests/test_cases/jde_full.json`
   - **A path** — only G has data → `tests/test_cases/jde_a_path.json`
   - **B path** — only I has data → `tests/test_cases/jde_b_path.json`
   - Rows with both G and I empty are skipped
3. **Execute** — For each valid row, the dashboard loads the path-specific JSON and runs its steps with `{{data.xxx}}` templates resolved from the row. All iterations reuse the same logged-in browser session.
4. **Results** — A live table shows pass/fail status, duration, tokens, and the path/JSON used per iteration. Click "View Full HTML Report" for the detailed report.

**Excel column contract** (columns A–K, fixed by the dashboard):

| Col | Variable | Required | Notes |
|----|----------|:---:|-------|
| A | `user_story` | No | |
| B | `app_report` | Yes | Must start with `R` or `P` |
| C | `current_version` | Yes | |
| D | `new_version` | Yes | |
| E | `current_version_title` | No | |
| F | `new_version_title` | Yes | |
| G | `left_operand` | No | Path detection |
| H | `data_new` | No | |
| I | `tab` | No | Path detection |
| J | `option_number` | No | |
| K | `processing_new` | No | |

**Run folder per session** — every dashboard run creates `logs/MM-DD-YYYY_HH_MM_JDE_Dashboard/` containing:
- `uploaded_<filename>.xlsx` — the Excel file used
- `report_<suite_name>.html` — final HTML report
- `<suite_id>_<timestamp>.jsonl` — structured per-step log
- `screenshots/` — per-iteration screenshots

### 6. Optional — LLM Proxy Servers

Run a proxy server in front of the engines so the LLM calls go through a corporate endpoint (Globant GeAI or JNJ Azure) instead of direct OpenAI/Anthropic.

```bash
# Globant GeAI proxy (default port 3456)
python main.py proxy

# JNJ Azure proxy (default port 3457) — requires VPN
python main.py proxy-jnj
```

Engines auto-detect the proxy via `STAGEHAND_SERVER_URL` in `.env`. If the proxy is down, they fall back to direct LLM calls using `LLM_API_KEY`. See `.env.example` for proxy config.

---

## JSON Schema — Test Case Structure

### Full TestSuiteRequest

```json
{
  "suite_id": "SUITE-001",
  "suite_name": "My Test Suite",
  "environment": "staging",
  "browser": "chromium",
  "headless": true,
  "llm_provider": "anthropic",
  "llm_model": "claude-sonnet-4-20250514",
  "parallel": false,
  "test_cases": [ ... ]
}
```

### TestCase

```json
{
  "test_id": "TC-001",
  "name": "Login flow",
  "description": "Validate user can log in",
  "tags": ["smoke", "login"],
  "platform": "sap_fiori",
  "base_url": "https://app.example.com",
  "preconditions": "User account exists",
  "steps": [ ... ],
  "expected_result": "User sees dashboard",
  "priority": "critical"
}
```

### TestStep with variable data

```json
{
  "step_id": "S001",
  "name": "Enter username",
  "action": "type",
  "target": {
    "description": "the username input field",
    "selector": "#username",
    "selector_strategy": "ai",
    "iframe": null,
    "shadow_host": null
  },
  "data": {
    "value": "test_user@example.com",
    "clear_before": true,
    "sensitive": false
  },
  "timeout_ms": 15000,
  "retry_count": 2,
  "continue_on_failure": false,
  "pre_wait_ms": 1000,
  "screenshot_on_failure": true
}
```

### Supported Actions

| Action | Description | Requires Target | Requires Data |
|--------|-------------|:---:|:---:|
| `navigate` | Go to URL | No | Yes (URL) |
| `click` | Left-click element | Yes | No |
| `right_click` | Right-click element (opens context menu, JDE row actions, etc.) | Yes | No |
| `type` | Type text into field | Yes | Yes (text) |
| `select` | Select dropdown option | Yes | Yes (option) |
| `wait` | Wait for element visible | Yes | No |
| `assert_visible` | Assert element/text is visible (also searches iframes for plain text) | Yes | No |
| `assert_text` | Assert element contains text | Yes | Yes (expected) |
| `assert_value` | Assert element value | Yes | Yes (expected) |
| `extract` | Extract text from element | Yes | No |
| `key_press` | Press a key or key combo (`Enter`, `Ctrl+F8`, `Tab`, etc.) — optionally scoped to a target element | Optional | Yes (key combo) |
| `check_error` | Check for an error banner on the page (walks all iframes). Fails the iteration with the extracted error text if found. | Yes | No |
| `screenshot` | Capture page screenshot | No | No |
| `custom` | Custom AI action | Yes | Optional |

#### `key_press` — examples

```json
{ "action": "key_press", "data": { "value": "Enter" } }
{ "action": "key_press", "data": { "value": "Control+Alt+I" } }
{ "action": "key_press", "data": { "value": "Tab" },
  "target": { "description": "the search input", "selector": "input[name='q']", "selector_strategy": "css" } }
```

Shorthand aliases auto-normalize to Playwright key names: `Ctrl` → `Control`, `Cmd/Win` → `Meta`, `Esc` → `Escape`, `Del` → `Delete`, `Up/Down/Left/Right` → `Arrow*`, etc.

#### `check_error` — examples

```json
{
  "step_id": "S020",
  "name": "Detect JDE error banner",
  "action": "check_error",
  "target": {
    "description": "JDE error message container",
    "selector": "#INYFEContent",
    "selector_strategy": "css"
  },
  "timeout_ms": 3000,
  "screenshot_on_failure": true
}
```

- **No error present** → step passes silently, execution continues
- **Error found with text** → step fails, iteration stops (unless `continue_on_failure: true`), and the extracted error text is captured into the step's `error_message` and shown in the HTML report
- Walks the main page plus every iframe automatically — no need to specify iframe

### Selector Strategies

| Strategy | When to use |
|----------|------------|
| `ai` | **Default.** LLM (Stagehand / direct OpenAI-compatible API) resolves via page context + description. Best for SAP, unstable DOMs. |
| `css` | Stable CSS selectors (id, data-testid, classes) — fastest, 0 tokens |
| `xpath` | Complex DOM traversal (label-next-to-input patterns in SAP/JDE) |
| `text` | Match by visible text |
| `role` | ARIA role matching |
| `data_attr` | Custom data attributes |
| `ui5_stable` | SAP `data-ui5-stable` attribute |

#### Iframe support

Add `iframe` to the `target` to scope any selector strategy inside a frame — works with `css`, `xpath`, `ai`, and all fallbacks:

```json
"target": {
  "description": "Batch Application input",
  "selector": "#C0_11",
  "selector_strategy": "css",
  "iframe": "iframe#e1menuAppIframe"
}
```

Multiple comma-separated selectors are tried in order if the iframe name isn't known.

#### Element resolution chain (when `selector_strategy: "ai"`)

When the LLM's CSS selector doesn't match, the engine automatically tries additional strategies in order:

1. Raw LLM selector(s) — `selectors` array, plus `selector`
2. `get_by_label(...)` — for inputs with `<label for="...">`
3. `get_by_placeholder(...)` — for fields with placeholder text
4. `get_by_role("button"|"link"|"textbox", name=...)` — accessible name matching
5. `input[value="..."]` — for submit buttons (`<input type="submit" value="Sign In">`)
6. `get_by_text(...)` — plain text substring match
7. **Adjacent-input fallback** — finds a label text, then the next sibling / parent's next sibling / sibling `<td>` that contains an `<input>` (SAP/JDE label-next-to-input pattern)

### Supported Platforms

| Platform | Optimizations |
|----------|--------------|
| `sap_fiori` | 60s timeout, ui5-stable fallback, longer DOM settle |
| `sap_webgui` | 45s timeout, frame handling |
| `jde_e1` | 45s timeout, Control-ID selectors, iframe handling (`e1menuAppIframe`) |
| `salesforce` | 45s timeout, Lightning components |
| `dynamics_365` | 40s timeout |
| `generic_web` | 30s timeout, standard Playwright |
| `custom` or any string | Configurable — unknown platform names default to 30s timeout |

---

## HTML Report

The report includes:

- **Suite summary**: total tests, pass/fail counts, duration, pass rate
- **LLM info**: provider, model, total tokens consumed
- **Per-test cards** (expandable): all steps with status, timestamps, duration
- **Step details**: action type, resolved selector, AI tokens used, error messages
- **Screenshots**: linked for failed steps
- **Dark mode**: automatically adapts to system preference

---

## Logging System

### Console output (color-coded)

```
12:34:56 ▸ RUNNING [TC-001/S003] Click 'Save' button
12:34:57 ● PASS   [TC-001/S003] Click 'Save' button  (342ms) [150 tokens]
12:34:58 ✖ FAIL   [TC-001/S004] Assert text visible   (1200ms)
         ↳ Element not found: success message
```

### JSON structured logs

Each line in `logs/*.jsonl` is a complete JSON object:

```json
{
  "timestamp": "2025-03-20T12:34:57Z",
  "level": "INFO",
  "logger": "qa.hybrid_playwright",
  "message": "Click 'Save' button",
  "test_id": "TC-001",
  "step_id": "S003",
  "status": "PASS",
  "duration_ms": 342.5,
  "tokens": 150,
  "selector": "[data-ui5-stable='saveBtn']"
}
```

---

## Why SAP Needs AI-Powered Automation

SAP UI5/Fiori presents unique challenges for traditional automation:

1. **Dynamic IDs**: Generated at runtime (`__xmlview1--btn-42`), change between sessions
2. **Shadow DOM**: UI5 Web Components encapsulate elements in shadow trees
3. **Deep nesting**: 10+ levels of container divs typical
4. **Async rendering**: UI5 render cycles don't align with standard DOM ready events
5. **Class instability**: CSS classes change between UI5 releases
6. **Generated markup**: Views compile to DOM at runtime, no static HTML

Stagehand's LLM identification bypasses all of these by "seeing" the page like a human tester would — finding elements by their visual description rather than fragile DOM attributes.

---

## API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/execute` | POST | Synchronous suite execution |
| `/execute/async` | POST | Queue suite for background execution |
| `/status/{run_id}` | GET | Check async execution status |
| `/report/{run_id}` | GET | Download HTML report |
| `/schema/test-suite` | GET | JSON schema for TestSuiteRequest |
| `/schema/test-case` | GET | JSON schema for TestCase |

Interactive API docs available at `http://localhost:8000/docs` (Swagger UI).
