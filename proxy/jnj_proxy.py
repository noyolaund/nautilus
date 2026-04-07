"""JNJ Azure OpenAI Proxy Server.

Translates Stagehand /act, /observe, /extract requests into Azure OpenAI
chat completions calls targeting the JNJ corporate endpoint.

Mirrors the TypeScript proxy at RD-stagehand_coda-sap-automation/src/proxy-server-jnj.ts
with the same endpoint, auth (api-key header), retry logic, and health tracking.

Also exposes /chat/completions for direct OpenAI-compatible clients (Stagehand).

Environment variables:
    JNJ_AZURE_ENDPOINT        – Azure OpenAI base URL (default: https://genaiapimna.jnj.com/openai-chat)
    JNJ_AZURE_API_KEY         – API key
    JNJ_AZURE_DEPLOYMENT      – Model deployment name (default: gpt-4o)
    JNJ_AZURE_API_VERSION     – Azure API version (default: 2024-10-21)
    JNJ_PROXY_PORT            – Local port (default: 3457)
    JNJ_PROXY_REQUEST_TIMEOUT – Per-request timeout in seconds (default: 120)
    JNJ_PROXY_MAX_RETRIES     – Max retry attempts (default: 3)
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from rich.console import Console
from rich.text import Text

_console = Console(stderr=True)

# ---------------------------------------------------------------------------
# Configuration — mirrors proxy-server-jnj.ts defaults
# ---------------------------------------------------------------------------

JNJ_CONFIG = {
    "azure_endpoint": os.getenv("JNJ_AZURE_ENDPOINT", "https://genaiapimna.jnj.com/openai-chat"),
    "api_key": os.getenv("JNJ_AZURE_API_KEY", ""),
    "deployment": os.getenv("JNJ_AZURE_DEPLOYMENT", "gpt-4o"),
    "api_version": os.getenv("JNJ_AZURE_API_VERSION", "2024-10-21"),
    "port": int(os.getenv("JNJ_PROXY_PORT", "3457")),
    "request_timeout": int(os.getenv("JNJ_PROXY_REQUEST_TIMEOUT", "120")),
    "max_retries": int(os.getenv("JNJ_PROXY_MAX_RETRIES", "3")),
    "retry_base_delay": 1.0,
    "retry_max_delay": 10.0,
    "backoff_multiplier": 2,
    "max_error_threshold": 5,
}


def _get_azure_url() -> str:
    base = JNJ_CONFIG["azure_endpoint"].rstrip("/")
    deploy = JNJ_CONFIG["deployment"]
    version = JNJ_CONFIG["api_version"]
    return f"{base}/openai/deployments/{deploy}/chat/completions?api-version={version}"


# ---------------------------------------------------------------------------
# Console logging
# ---------------------------------------------------------------------------

def _log(icon: str, style: str, msg: str, detail: str = "") -> None:
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    text = Text()
    text.append(f"{ts} ", style="dim")
    text.append(f"{icon} JNJ ", style=f"bold {style}")
    text.append(msg)
    if detail:
        text.append(f" {detail}", style="dim")
    _console.print(text)


# ---------------------------------------------------------------------------
# Health tracking — mirrors TypeScript ProxyHealth
# ---------------------------------------------------------------------------

_health = {
    "status": "healthy",
    "last_error": None,
    "error_count": 0,
    "success_count": 0,
    "last_success_ts": None,
    "last_error_ts": None,
    "uptime": time.monotonic(),
}


def _update_health_success() -> None:
    _health["status"] = "healthy"
    _health["error_count"] = max(0, _health["error_count"] - 1)
    _health["success_count"] += 1
    _health["last_success_ts"] = datetime.now().isoformat()


def _update_health_error(error_msg: str) -> None:
    _health["error_count"] += 1
    _health["last_error"] = error_msg
    _health["last_error_ts"] = datetime.now().isoformat()
    threshold = JNJ_CONFIG["max_error_threshold"]
    if _health["error_count"] >= threshold:
        _health["status"] = "unhealthy"
    elif _health["error_count"] >= threshold // 2:
        _health["status"] = "degraded"


# ---------------------------------------------------------------------------
# Stagehand system prompts
# ---------------------------------------------------------------------------

_SYSTEM_ACT = """You are a browser automation agent. You will receive the current page structure followed by an action instruction.
Analyze the ACTUAL page content and find the REAL element that matches.
Respond with a JSON object: {"selector": "...", "description": "...", "success": true/false}
Only use selectors from actual attributes. Never guess. Respond ONLY with valid JSON."""

_SYSTEM_OBSERVE = """You are a browser automation agent. You will receive the current page structure followed by an instruction to find elements.
Respond with a JSON object: {"elements": [{"selector": "...", "description": "..."}]}
Only return selectors from actual attributes. Respond ONLY with valid JSON."""

_SYSTEM_EXTRACT = """You are a browser automation agent. You will receive page content and an extraction instruction.
Respond with a JSON object: {"text": "...", "value": "...", "data": ...}
Respond ONLY with valid JSON."""


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ActRequest(BaseModel):
    action: str = Field(..., min_length=1, max_length=10000)
    modelName: Optional[str] = None
    modelProvider: Optional[str] = None


class ObserveRequest(BaseModel):
    instruction: str = Field(..., min_length=1, max_length=10000)
    modelName: Optional[str] = None
    modelProvider: Optional[str] = None


class ExtractRequest(BaseModel):
    instruction: str = Field(..., min_length=1, max_length=10000)
    modelName: Optional[str] = None
    modelProvider: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    """OpenAI-compatible /chat/completions body (passthrough to Azure)."""
    model: Optional[str] = None
    messages: list[dict[str, Any]]
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


# ---------------------------------------------------------------------------
# Azure OpenAI caller with retries + exponential backoff
# ---------------------------------------------------------------------------

_http_client = httpx.AsyncClient(timeout=float(JNJ_CONFIG["request_timeout"]))


async def _call_azure(
    messages: list[dict[str, Any]],
    request_id: str,
    temperature: float = 0.1,
    max_tokens: int = 2048,
) -> dict[str, Any]:
    """Call Azure OpenAI with automatic retries and exponential backoff."""
    url = _get_azure_url()
    api_key = JNJ_CONFIG["api_key"]
    deployment = JNJ_CONFIG["deployment"]

    if not api_key:
        _log("✖", "red", "API key not configured — set JNJ_AZURE_API_KEY in .env")
        raise HTTPException(status_code=503, detail="JNJ API key not configured")

    payload = {
        "model": deployment,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    headers = {
        "Content-Type": "application/json",
        "api-key": api_key,
    }

    payload_size = len(json.dumps(payload))
    _log("↑", "blue", f"[{request_id}] POST {url}", f"| {payload_size} bytes | deployment={deployment}")

    last_error: Optional[Exception] = None
    max_retries = JNJ_CONFIG["max_retries"]

    for attempt in range(1, max_retries + 1):
        attempt_start = time.monotonic()
        _log("→", "cyan", f"[{request_id}] Attempt {attempt}/{max_retries}")

        try:
            resp = await _http_client.post(url, json=payload, headers=headers)
            elapsed_ms = (time.monotonic() - attempt_start) * 1000
            _log("·", "dim", f"[{request_id}] Response HTTP {resp.status_code} in {elapsed_ms:.0f}ms")

            if resp.status_code != 200:
                body = resp.text[:300]
                _log("✖", "red", f"[{request_id}] HTTP {resp.status_code}", f"| {body}")
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}", request=resp.request, response=resp
                )

            data = resp.json()

            # Token tracking
            usage = data.get("usage", {})
            prompt_tok = usage.get("prompt_tokens", 0)
            compl_tok = usage.get("completion_tokens", 0)
            total_tok = usage.get("total_tokens", 0) or (prompt_tok + compl_tok)
            _log("·", "dim", f"[{request_id}] Tokens: {prompt_tok}+{compl_tok}={total_tok}")

            _update_health_success()
            _log("←", "green", f"[{request_id}] Success in {elapsed_ms:.0f}ms", f"| {total_tok} tokens")
            return data

        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            last_error = exc
            error_msg = str(exc)[:150]
            _log("✖", "red", f"[{request_id}] Attempt {attempt} failed: {error_msg}")
            _update_health_error(error_msg)

            if attempt < max_retries:
                delay = min(
                    JNJ_CONFIG["retry_base_delay"] * (JNJ_CONFIG["backoff_multiplier"] ** (attempt - 1)),
                    JNJ_CONFIG["retry_max_delay"],
                )
                _log("⏳", "yellow", f"[{request_id}] Retrying in {delay:.0f}s...")
                await asyncio.sleep(delay)

    _log("✖", "red", f"[{request_id}] All {max_retries} attempts failed")
    raise HTTPException(
        status_code=502,
        detail=f"JNJ Azure proxy: all {max_retries} retries exhausted. Last error: {last_error}",
    )


async def _call_stagehand_op(
    system_prompt: str,
    user_message: str,
    operation: str,
) -> dict[str, Any]:
    """Wrap a Stagehand operation (act/observe/extract) as a chat completion."""
    request_id = f"{int(time.time())}-{uuid.uuid4().hex[:6]}"
    _log("→", "cyan", f"[{request_id}] {operation}()", f"| {user_message[:120]}")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    data = await _call_azure(messages, request_id)

    # Parse assistant content
    choices = data.get("choices", [])
    if not choices:
        return {"tokens": 0}

    content = choices[0].get("message", {}).get("content", "").strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        _log("⚠", "yellow", f"[{request_id}] Non-JSON response: {content[:100]}")
        result = {"text": content}

    # Inject real token usage
    usage = data.get("usage", {})
    prompt_tok = usage.get("prompt_tokens", 0)
    compl_tok = usage.get("completion_tokens", 0)
    total = usage.get("total_tokens", 0) or (prompt_tok + compl_tok)
    result["tokens"] = total

    # Log parsed result
    if operation == "act":
        sel = result.get("selector", "")
        _log("←", "green", f"[{request_id}] {operation}()", f"| selector={sel[:80]}" if sel else "| no selector")
    elif operation == "observe":
        els = result.get("elements", [])
        _log("←", "green", f"[{request_id}] {operation}()", f"| {len(els)} elements found")
        for el in els[:3]:
            _log("  ↳", "dim", el.get("selector", "?"), f"— {el.get('description', '')[:50]}")
    elif operation == "extract":
        _log("←", "green", f"[{request_id}] {operation}()", f"| text='{result.get('text', '')[:60]}'")

    return result


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

_request_counts: dict[str, list[float]] = defaultdict(list)
_RATE_LIMIT = 30


def _check_rate_limit(client_ip: str) -> bool:
    now = time.monotonic()
    window = [t for t in _request_counts[client_ip] if now - t < 60]
    _request_counts[client_ip] = window
    if len(window) >= _RATE_LIMIT:
        return False
    _request_counts[client_ip].append(now)
    return True


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

def create_jnj_proxy_app() -> FastAPI:

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _console.print(Text.from_markup(
            f"\n[bold blue]╔══════════════════════════════════════════════════════════════╗[/]\n"
            f"[bold blue]║[/]  [bold white]JNJ Azure OpenAI Proxy Server[/]                              [bold blue]║[/]\n"
            f"[bold blue]║[/]  Port: {JNJ_CONFIG['port']:<52}[bold blue]║[/]\n"
            f"[bold blue]║[/]  Endpoint: {JNJ_CONFIG['azure_endpoint']:<48}[bold blue]║[/]\n"
            f"[bold blue]║[/]  Deployment: {JNJ_CONFIG['deployment']:<46}[bold blue]║[/]\n"
            f"[bold blue]║[/]  API Version: {JNJ_CONFIG['api_version']:<45}[bold blue]║[/]\n"
            f"[bold blue]║[/]  API Key: {'YES' if JNJ_CONFIG['api_key'] else 'NO':<50}[bold blue]║[/]\n"
            f"[bold blue]║[/]  URL: {_get_azure_url()[:52]:<52}[bold blue]║[/]\n"
            f"[bold blue]║[/]                                                            [bold blue]║[/]\n"
            f"[bold blue]║[/]  Features:                                                 [bold blue]║[/]\n"
            f"[bold blue]║[/]  - Automatic retry with exponential backoff                [bold blue]║[/]\n"
            f"[bold blue]║[/]  - Health monitoring (healthy/degraded/unhealthy)           [bold blue]║[/]\n"
            f"[bold blue]║[/]  - Azure OpenAI (JNJ corporate endpoint)                   [bold blue]║[/]\n"
            f"[bold blue]║[/]  - Stagehand act/observe/extract + /chat/completions       [bold blue]║[/]\n"
            f"[bold blue]╚══════════════════════════════════════════════════════════════╝[/]\n"
        ))
        yield

    app = FastAPI(
        title="JNJ Azure OpenAI Proxy",
        version="1.0.0",
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def security_and_rate_limit(request: Request, call_next):
        client_ip = request.client.host if request.client else "unknown"
        if not _check_rate_limit(client_ip):
            return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        return response

    # --- Health endpoints ---

    @app.get("/health")
    async def health():
        uptime_s = int(time.monotonic() - _health["uptime"])
        is_healthy = _health["error_count"] < JNJ_CONFIG["max_error_threshold"]
        return {
            "status": _health["status"],
            "error_count": _health["error_count"],
            "success_count": _health["success_count"],
            "last_error": _health["last_error"],
            "last_success_timestamp": _health["last_success_ts"],
            "last_error_timestamp": _health["last_error_ts"],
            "uptime_seconds": uptime_s,
            "healthy": is_healthy,
            "endpoint": JNJ_CONFIG["azure_endpoint"],
            "deployment": JNJ_CONFIG["deployment"],
        }

    @app.post("/health/reset")
    async def health_reset():
        _health["error_count"] = 0
        _health["success_count"] = 0
        _health["last_error"] = None
        _health["status"] = "healthy"
        return {"message": "Health metrics reset"}

    # --- /chat/completions passthrough (Stagehand-compatible) ---

    @app.post("/chat/completions")
    async def chat_completions(body: ChatCompletionRequest):
        """OpenAI-compatible passthrough — routes everything to JNJ Azure."""
        request_id = f"{int(time.time())}-{uuid.uuid4().hex[:6]}"
        _log("→", "cyan", f"[{request_id}] /chat/completions", f"| model={body.model}")

        messages = body.messages
        data = await _call_azure(
            messages,
            request_id,
            temperature=body.temperature or 0.1,
            max_tokens=body.max_tokens or 2048,
        )
        return data

    # --- Stagehand act/observe/extract ---

    @app.post("/act")
    async def act(act_req: ActRequest):
        result = await _call_stagehand_op(_SYSTEM_ACT, act_req.action, "act")
        return {
            "success": result.get("success", True),
            "selector": result.get("selector", ""),
            "action_performed": result.get("action_performed", act_req.action),
            "description": result.get("description", ""),
            "tokens": result.get("tokens", 0),
        }

    @app.post("/observe")
    async def observe(observe_req: ObserveRequest):
        result = await _call_stagehand_op(_SYSTEM_OBSERVE, observe_req.instruction, "observe")
        return {
            "elements": result.get("elements", []),
            "tokens": result.get("tokens", 0),
        }

    @app.post("/extract")
    async def extract(extract_req: ExtractRequest):
        result = await _call_stagehand_op(_SYSTEM_EXTRACT, extract_req.instruction, "extract")
        return {
            "text": result.get("text", ""),
            "value": result.get("value", ""),
            "data": result.get("data"),
            "tokens": result.get("tokens", 0),
        }

    return app


jnj_proxy_app = create_jnj_proxy_app()
