"""HTML report generator using the project's established report layout.

All dynamic content is escaped via Jinja2 auto-escaping to prevent XSS.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from markupsafe import escape
from jinja2 import Environment, BaseLoader

from models.schemas import StepStatus, SuiteResult, TestStatus

# ---------------------------------------------------------------------------
# Report template — matches the existing report layout exactly
# ---------------------------------------------------------------------------

_TEMPLATE_SOURCE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>QA Report — {{ suite.suite_name }}</title>
<style>
:root {
    --bg: #fafaf8;
    --card: #ffffff;
    --border: #e5e4df;
    --text: #2c2c2a;
    --muted: #73726c;
    --pass: #1d9e75;
    --pass-bg: #e1f5ee;
    --fail: #e24b4a;
    --fail-bg: #fcebeb;
    --error: #ba7517;
    --error-bg: #faeeda;
    --skip: #888780;
    --skip-bg: #f1efe8;
    --accent: #534ab7;
    --accent-bg: #eeedfe;
    --mono: 'SF Mono', 'Fira Code', 'JetBrains Mono', monospace;
}
@media (prefers-color-scheme: dark) {
    :root {
        --bg: #1a1a1a;
        --card: #242424;
        --border: #3a3a38;
        --text: #e0ddd5;
        --muted: #9c9a92;
        --pass-bg: #0a2e20;
        --fail-bg: #2e1212;
        --error-bg: #2e2008;
        --skip-bg: #2a2a28;
        --accent-bg: #1e1d30;
    }
}
* { margin:0; padding:0; box-sizing:border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
    padding: 2rem;
    max-width: 1200px;
    margin: 0 auto;
}
.header {
    margin-bottom: 2rem;
    padding-bottom: 1.5rem;
    border-bottom: 1px solid var(--border);
}
.header h1 {
    font-size: 1.5rem;
    font-weight: 600;
    margin-bottom: 0.5rem;
}
.header .subtitle {
    color: var(--muted);
    font-size: 0.875rem;
}
.metrics {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 12px;
    margin-bottom: 2rem;
}
.metric {
    background: var(--card);
    border: 0.5px solid var(--border);
    border-radius: 10px;
    padding: 1rem 1.25rem;
}
.metric-label {
    font-size: 0.75rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 4px;
}
.metric-value {
    font-size: 1.5rem;
    font-weight: 600;
}
.metric-value.pass { color: var(--pass); }
.metric-value.fail { color: var(--fail); }
.metric-value.accent { color: var(--accent); }
.llm-info {
    background: var(--accent-bg);
    border: 0.5px solid var(--border);
    border-radius: 10px;
    padding: 1rem 1.25rem;
    margin-bottom: 2rem;
    display: flex;
    gap: 2rem;
    flex-wrap: wrap;
    font-size: 0.875rem;
}
.llm-info strong { color: var(--accent); }
.test-card {
    background: var(--card);
    border: 0.5px solid var(--border);
    border-radius: 10px;
    margin-bottom: 8px;
    overflow: hidden;
}
.test-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1rem 1.25rem;
    cursor: pointer;
    transition: background 0.15s;
}
.test-header:hover { background: var(--bg); }
.test-title {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 0.9rem;
}
.test-meta {
    display: flex;
    gap: 16px;
    font-size: 0.8rem;
    color: var(--muted);
    align-items: center;
}
.chevron {
    transition: transform 0.2s;
    font-size: 0.9rem;
}
.chevron.open { transform: rotate(90deg); }
.test-body {
    border-top: 0.5px solid var(--border);
    padding: 0;
}
.step-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.8rem;
}
.step-table th {
    text-align: left;
    padding: 8px 12px;
    background: var(--bg);
    font-weight: 500;
    color: var(--muted);
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    white-space: nowrap;
}
.step-table td {
    padding: 8px 12px;
    border-top: 0.5px solid var(--border);
    vertical-align: top;
}
.step-id { font-family: var(--mono); color: var(--muted); font-size: 0.75rem; white-space: nowrap; }
.ts-col { font-family: var(--mono); font-size: 0.75rem; white-space: nowrap; }
.dur-col { font-family: var(--mono); font-size: 0.75rem; white-space: nowrap; }
code {
    font-family: var(--mono);
    font-size: 0.75rem;
    background: var(--bg);
    padding: 2px 6px;
    border-radius: 4px;
}
.status-badge {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 8px;
    border-radius: 6px;
    font-weight: 500;
    font-size: 0.75rem;
    white-space: nowrap;
}
.status-pass { background: var(--pass-bg); color: var(--pass); }
.status-fail { background: var(--fail-bg); color: var(--fail); }
.status-error { background: var(--error-bg); color: var(--error); }
.status-skip { background: var(--skip-bg); color: var(--skip); }
.status-pass-bg td { background: transparent; }
.status-fail-bg td { background: var(--fail-bg); }
.status-error-bg td { background: var(--error-bg); }
.step-error {
    margin-top: 4px;
    padding: 4px 8px;
    background: var(--fail-bg);
    border-radius: 4px;
    color: var(--fail);
    font-size: 0.75rem;
    font-family: var(--mono);
    word-break: break-all;
}
.token-badge {
    background: var(--accent-bg);
    color: var(--accent);
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 0.7rem;
    font-weight: 500;
}
.selector-info {
    color: var(--muted);
    font-family: var(--mono);
    font-size: 0.7rem;
    display: block;
    margin-bottom: 4px;
}
.screenshot-link {
    font-size: 0.75rem;
    color: var(--accent);
    text-decoration: none;
    display: block;
    margin-top: 4px;
}
.footer {
    margin-top: 2rem;
    padding-top: 1rem;
    border-top: 0.5px solid var(--border);
    font-size: 0.75rem;
    color: var(--muted);
    text-align: center;
}
</style>
</head>
<body>

<div class="header">
    <h1>QA Report — {{ suite.suite_name }}</h1>
    <div class="subtitle">
        Suite: {{ suite.suite_id }} ·
        Environment: {{ suite.environment }} ·
        Browser: {{ suite.browser }} ·
        Generated: {{ generated_at }}
    </div>
</div>

<div class="metrics">
    <div class="metric">
        <div class="metric-label">Total tests</div>
        <div class="metric-value">{{ suite.total_tests }}</div>
    </div>
    <div class="metric">
        <div class="metric-label">Passed</div>
        <div class="metric-value pass">{{ suite.passed }}</div>
    </div>
    <div class="metric">
        <div class="metric-label">Failed</div>
        <div class="metric-value fail">{{ suite.failed }}</div>
    </div>
    <div class="metric">
        <div class="metric-label">Errors</div>
        <div class="metric-value fail">{{ suite.errors }}</div>
    </div>
    <div class="metric">
        <div class="metric-label">Pass rate</div>
        <div class="metric-value {{ 'pass' if suite.pass_rate >= 80 else 'fail' }}">{{ "%.1f"|format(suite.pass_rate) }}%</div>
    </div>
    <div class="metric">
        <div class="metric-label">Duration</div>
        <div class="metric-value">{{ "%.1f"|format(suite.total_duration_ms / 1000) }}s</div>
    </div>
    <div class="metric">
        <div class="metric-label">Total tokens</div>
        <div class="metric-value accent">{{ suite.total_tokens|commaformat }}</div>
    </div>
</div>

<div class="llm-info">
    <div><strong>LLM Provider:</strong> {{ suite.llm_provider }}</div>
    <div><strong>Model:</strong> {{ suite.llm_model }}</div>
    <div><strong>Total tokens consumed:</strong> {{ suite.total_tokens|commaformat }}</div>
    <div><strong>Started:</strong> {{ suite.started_at.strftime('%Y-%m-%d %H:%M:%S') if suite.started_at else 'N/A' }}</div>
    <div><strong>Finished:</strong> {{ suite.finished_at.strftime('%Y-%m-%d %H:%M:%S') if suite.finished_at else 'N/A' }}</div>
</div>

{% for test in suite.test_results %}
        <div class="test-card">
            <div class="test-header" onclick="toggleTest('test-{{ loop.index0 }}')">
                <div class="test-title">
                    {% if test.status.value == 'pass' %}
                    <span class="status-badge status-pass">● PASS</span>
                    {% elif test.status.value == 'fail' %}
                    <span class="status-badge status-fail">✖ FAIL</span>
                    {% else %}
                    <span class="status-badge status-error">⚠ ERROR</span>
                    {% endif %}
                    <strong>{{ test.test_id }}</strong> — {{ test.name }}
                </div>
                <div class="test-meta">
                    <span>{{ test.steps|length }} steps</span>
                    <span>{{ test.duration_ms|int }}ms</span>
                    <span>{{ test.total_tokens }} tokens</span>
                    <span class="chevron" id="chevron-test-{{ loop.index0 }}">▸</span>
                </div>
            </div>
            <div class="test-body" id="test-{{ loop.index0 }}" style="display:none">
                <table class="step-table">
                    <thead>
                        <tr>
                            <th>Step</th>
                            <th>Name</th>
                            <th>Action</th>
                            <th>Status</th>
                            <th>Timestamp</th>
                            <th>Duration</th>
                            <th>Tokens</th>
                            <th>Details</th>
                        </tr>
                    </thead>
                    <tbody>
            {% for step in test.steps %}
            <tr class="step-row status-{{ step.status.value }}-bg">
                <td class="step-id">{{ step.step_id }}</td>
                <td>{{ step.name }}</td>
                <td><code>{{ step.action.value }}</code></td>
                <td>
                    {% if step.status.value == 'pass' %}
                    <span class="status-badge status-pass">● PASS</span>
                    {% elif step.status.value == 'fail' %}
                    <span class="status-badge status-fail">✖ FAIL</span>
                    {% elif step.status.value == 'error' %}
                    <span class="status-badge status-error">⚠ ERROR</span>
                    {% else %}
                    <span class="status-badge status-skip">○ SKIP</span>
                    {% endif %}
                </td>
                <td class="ts-col">{% if step.started_at %}{{ step.started_at.strftime('%H:%M:%S.') }}{{ '%03d' % (step.started_at.microsecond // 1000) }}{% endif %}</td>
                <td class="dur-col">{{ step.duration_ms|int }}ms</td>
                <td>{% if step.tokens_used %}<span class="token-badge">{{ step.tokens_used }} tok</span>{% endif %}</td>
                <td>
                    {% if step.resolved_selector %}<span class="selector-info">{{ step.resolved_selector }}</span>{% endif %}
                    {% if step.error_message %}<div class="step-error">{{ step.error_message }}</div>{% endif %}
                    {% if step.screenshot_path %}<a href="{{ step.screenshot_path }}" class="screenshot-link">📸 screenshot</a>{% endif %}
                </td>
            </tr>
            {% endfor %}</tbody>
                </table>
            </div>
        </div>
{% endfor %}

<div class="footer">
    QA Automation Framework Report · Engine: {{ suite.engine_type.value }} ·
    {{ generated_at }}
</div>

<script>
function toggleTest(id) {
    const el = document.getElementById(id);
    const chevron = document.getElementById('chevron-' + id);
    if (el.style.display === 'none') {
        el.style.display = 'block';
        chevron.classList.add('open');
    } else {
        el.style.display = 'none';
        chevron.classList.remove('open');
    }
}
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_report(suite: SuiteResult, output_dir: str = ".", suite_name: str = "") -> str:
    """Render the HTML report and write it to *output_dir*.

    Returns the absolute path to the generated file.
    """
    env = Environment(loader=BaseLoader(), autoescape=True)
    env.filters["commaformat"] = lambda v: f"{int(v):,}"
    template = env.from_string(_TEMPLATE_SOURCE)

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html = template.render(suite=suite, generated_at=generated_at)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    safe_name = re.sub(r"[^\w\-.]", "_", suite_name or suite.suite_name)[:60]
    filename = f"report_{safe_name}.html"
    filepath = out / filename

    filepath.write_text(html, encoding="utf-8")
    return str(filepath.resolve())
