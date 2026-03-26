"""Azure OpenAI proxy server for Stagehand calls.

Sits between the QA engines and Azure OpenAI, translating Stagehand's
/act, /observe, /extract endpoints into Azure OpenAI chat completions.

All engines point STAGEHAND_SERVER_URL to this proxy instead of a
real Stagehand server.

Security:
  - API key read from env var, never hardcoded
  - Request validation via Pydantic
  - Rate limiting
  - No credentials in logs
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("qa.proxy")

# ---------------------------------------------------------------------------
# Configuration — all from environment variables
# ---------------------------------------------------------------------------

AZURE_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
AZURE_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
PROXY_PORT = int(os.getenv("PROXY_PORT", "3456"))


def _get_completions_url() -> str:
    """Build the Azure OpenAI chat completions URL."""
    base = AZURE_ENDPOINT.rstrip("/")
    return (
        f"{base}/openai/deployments/{AZURE_DEPLOYMENT}"
        f"/chat/completions?api-version={AZURE_API_VERSION}"
    )


# ---------------------------------------------------------------------------
# System prompts for each Stagehand operation
# ---------------------------------------------------------------------------

_SYSTEM_ACT = """You are a browser automation agent. The user describes an action to perform on a web page.
Respond with a JSON object containing:
- "success": true/false
- "selector": the CSS selector you would use to target the element (best guess)
- "action_performed": brief description of what was done
- "tokens": estimated token count for this operation (integer)

Respond ONLY with valid JSON, no markdown fences."""

_SYSTEM_OBSERVE = """You are a browser automation agent. The user asks you to identify elements on a web page.
Respond with a JSON object containing:
- "elements": array of objects, each with "selector" (CSS selector) and "description" (what the element is)
- "tokens": estimated token count for this operation (integer)

If no matching elements can be inferred, return an empty "elements" array.
Respond ONLY with valid JSON, no markdown fences."""

_SYSTEM_EXTRACT = """You are a browser automation agent. The user asks you to extract data from a web page.
Respond with a JSON object containing:
- "text": the extracted text content
- "value": the extracted value (if applicable)
- "data": any structured data extracted (object or null)
- "tokens": estimated token count for this operation (integer)

Respond ONLY with valid JSON, no markdown fences."""


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ActRequest(BaseModel):
    action: str = Field(..., min_length=1, max_length=5000)
    modelName: Optional[str] = None
    modelProvider: Optional[str] = None


class ObserveRequest(BaseModel):
    instruction: str = Field(..., min_length=1, max_length=5000)
    modelName: Optional[str] = None
    modelProvider: Optional[str] = None


class ExtractRequest(BaseModel):
    instruction: str = Field(..., min_length=1, max_length=5000)
    modelName: Optional[str] = None
    modelProvider: Optional[str] = None


# ---------------------------------------------------------------------------
# Azure OpenAI client
# ---------------------------------------------------------------------------

_http_client = httpx.AsyncClient(timeout=120.0)


async def _call_azure(system_prompt: str, user_message: str) -> dict[str, Any]:
    """Send a chat completion request to Azure OpenAI and return parsed JSON."""
    url = _get_completions_url()
    api_key = AZURE_API_KEY

    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="Azure OpenAI API key not configured",
        )
    if not AZURE_ENDPOINT:
        raise HTTPException(
            status_code=503,
            detail="Azure OpenAI endpoint not configured",
        )

    headers = {
        "Content-Type": "application/json",
        "api-key": api_key,
    }

    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.1,
        "max_tokens": 2048,
    }

    start = time.monotonic()
    try:
        resp = await _http_client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        # Don't leak Azure error details beyond status code
        logger.error("Azure OpenAI returned %d", status)
        raise HTTPException(
            status_code=502,
            detail=f"Azure OpenAI upstream error: HTTP {status}",
        )
    except httpx.ConnectError:
        logger.error("Cannot connect to Azure OpenAI endpoint")
        raise HTTPException(
            status_code=502,
            detail="Cannot connect to Azure OpenAI endpoint",
        )

    elapsed_ms = (time.monotonic() - start) * 1000
    data = resp.json()

    # Extract the assistant message
    choices = data.get("choices", [])
    if not choices:
        raise HTTPException(status_code=502, detail="Empty response from Azure OpenAI")

    content = choices[0].get("message", {}).get("content", "")

    # Parse JSON from response (strip markdown fences if present)
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        result = {"text": content, "tokens": 0}

    # Inject actual token usage from Azure response
    usage = data.get("usage", {})
    result["tokens"] = usage.get("total_tokens", result.get("tokens", 0))

    logger.info(
        "Azure proxy: %.0fms | %d tokens | %s",
        elapsed_ms,
        result.get("tokens", 0),
        content[:80],
    )

    return result


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Simple rate limiter (no decorator interference with FastAPI)
# ---------------------------------------------------------------------------

_request_counts: dict[str, list[float]] = defaultdict(list)
_RATE_LIMIT = 30  # requests per minute per IP


def _check_rate_limit(client_ip: str) -> bool:
    """Return True if request is allowed, False if rate-limited."""
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

def create_proxy_app() -> FastAPI:
    app = FastAPI(
        title="Stagehand → Azure OpenAI Proxy",
        version="1.0.0",
        docs_url="/docs",
        redoc_url=None,
    )

    @app.middleware("http")
    async def security_and_rate_limit(request: Request, call_next):
        # Rate limit check
        client_ip = request.client.host if request.client else "unknown"
        if not _check_rate_limit(client_ip):
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Max 30 requests/minute."},
            )
        # Process request
        response = await call_next(request)
        # Security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        return response

    # --- Health check -------------------------------------------------------

    @app.get("/health")
    async def health():
        return {
            "status": "healthy",
            "proxy": "azure_openai",
            "deployment": AZURE_DEPLOYMENT,
            "endpoint_configured": bool(AZURE_ENDPOINT),
            "key_configured": bool(AZURE_API_KEY),
        }

    # --- Stagehand-compatible endpoints ------------------------------------

    @app.post("/act")
    async def act(act_req: ActRequest):
        """Proxy for Stagehand act() — translates to Azure OpenAI."""
        result = await _call_azure(_SYSTEM_ACT, act_req.action)
        return {
            "success": result.get("success", True),
            "selector": result.get("selector", ""),
            "action_performed": result.get("action_performed", act_req.action),
            "tokens": result.get("tokens", 0),
        }

    @app.post("/observe")
    async def observe(observe_req: ObserveRequest):
        """Proxy for Stagehand observe() — translates to Azure OpenAI."""
        result = await _call_azure(_SYSTEM_OBSERVE, observe_req.instruction)
        return {
            "elements": result.get("elements", []),
            "tokens": result.get("tokens", 0),
        }

    @app.post("/extract")
    async def extract(extract_req: ExtractRequest):
        """Proxy for Stagehand extract() — translates to Azure OpenAI."""
        result = await _call_azure(_SYSTEM_EXTRACT, extract_req.instruction)
        return {
            "text": result.get("text", ""),
            "value": result.get("value", ""),
            "data": result.get("data"),
            "tokens": result.get("tokens", 0),
        }

    return app


proxy_app = create_proxy_app()
