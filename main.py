"""CLI entry point for the QA Automation Framework.

Usage:
    python main.py run <test-file.json> --engine hybrid|ai_native
    python main.py serve [--host HOST] [--port PORT]
    python main.py proxy [--port PORT]
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

load_dotenv()


@click.group()
def cli():
    """QA Automation Framework — AI-powered test automation."""


@cli.command()
@click.argument("test_file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--engine",
    type=click.Choice(["hybrid", "ai_native"], case_sensitive=False),
    default="hybrid",
    help="Engine type: hybrid (Playwright + AI fallback) or ai_native (full Stagehand).",
)
@click.option("--output", "-o", default=".", help="Directory for the HTML report.")
def run(test_file: Path, engine: str, output: str):
    """Execute a test suite from a JSON file."""
    from models.schemas import EngineType, TestSuiteRequest
    from engines.hybrid_playwright_engine import HybridPlaywrightEngine
    from engines.stagehand_ai_engine import StagehandAIEngine
    from reports.html_report import generate_report

    raw = json.loads(test_file.read_text(encoding="utf-8"))
    suite_request = TestSuiteRequest(**raw)

    engine_type = EngineType.AI_NATIVE if engine == "ai_native" else EngineType.HYBRID
    if engine_type == EngineType.AI_NATIVE:
        executor = StagehandAIEngine(suite_request)
    else:
        executor = HybridPlaywrightEngine(suite_request)

    click.echo(f"Running suite '{suite_request.suite_name}' with {engine} engine...")
    result = asyncio.run(executor.run())

    # Save report in the engine's run folder (logs/MM-DD-YYYY_HH_MM_test_name/)
    run_dir = str(executor._run_dir)
    report_path = generate_report(result, output_dir=run_dir)
    click.echo()
    click.echo(f"Total: {result.total_tests}  |  "
               f"Passed: {result.passed}  |  "
               f"Failed: {result.failed}  |  "
               f"Pass rate: {result.pass_rate:.1f}%")
    click.echo(f"Tokens used: {result.total_tokens:,}")
    click.echo(f"Duration: {result.total_duration_ms / 1000:.1f}s")
    click.echo(f"Report: {report_path}")

    sys.exit(0 if result.failed == 0 and result.errors == 0 else 1)


@cli.command()
@click.option("--host", default="127.0.0.1", help="Bind host.")
@click.option("--port", default=8000, type=int, help="Bind port.")
def serve(host: str, port: int):
    """Start the FastAPI REST API server."""
    import uvicorn
    click.echo(f"Starting API server on {host}:{port}")
    click.echo(f"API docs at http://{host}:{port}/docs")
    uvicorn.run("api.service:app", host=host, port=port, log_level="info")


@cli.command()
@click.option("--host", default="127.0.0.1", help="Bind host.")
@click.option(
    "--port",
    default=None,
    type=int,
    help="Bind port (default: PROXY_PORT env var or 3456).",
)
def proxy(host: str, port: int | None):
    """Start the Azure OpenAI proxy server for Stagehand calls."""
    import os
    import uvicorn
    bind_port = port or int(os.getenv("PROXY_PORT", "3456"))
    click.echo(f"Starting Azure OpenAI proxy on {host}:{bind_port}")
    click.echo(f"Engines should set STAGEHAND_SERVER_URL=http://{host}:{bind_port}")
    click.echo(f"Proxy docs at http://{host}:{bind_port}/docs")
    uvicorn.run("proxy.azure_proxy:proxy_app", host=host, port=bind_port, log_level="info")


@cli.command(name="proxy-jnj")
@click.option("--host", default="127.0.0.1", help="Bind host.")
@click.option(
    "--port",
    default=None,
    type=int,
    help="Bind port (default: JNJ_PROXY_PORT env var or 3457).",
)
def proxy_jnj(host: str, port: int | None):
    """Start the JNJ Azure OpenAI proxy server (Cloud PC)."""
    import os
    import uvicorn
    bind_port = port or int(os.getenv("JNJ_PROXY_PORT", "3457"))
    click.echo(f"Starting JNJ Azure proxy on {host}:{bind_port}")
    click.echo(f"Engines should set STAGEHAND_SERVER_URL=http://{host}:{bind_port}")
    click.echo(f"JNJ Proxy docs at http://{host}:{bind_port}/docs")
    uvicorn.run("proxy.jnj_proxy:jnj_proxy_app", host=host, port=bind_port, log_level="info")


if __name__ == "__main__":
    cli()
