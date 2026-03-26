"""
Template Resolver — Resolves {{data.xxx}} placeholders in test step values.

Template syntax:
    {{data.variable_name}}     → Direct variable lookup from current row
    {{data.sheet.variable}}    → Variable from a specific sheet
    {{row_index}}              → Current Excel row number
    {{iteration}}              → Current iteration number (1-based)
    {{env.VAR_NAME}}           → Environment variable
    {{now}}                    → Current timestamp
    {{now:%Y-%m-%d}}           → Formatted current timestamp

Examples in a test step JSON:
    "value": "{{data.vendor_number}}"         → "10001"
    "value": "PO-{{data.po_number}}-{{now:%Y%m%d}}" → "PO-4500012345-20260325"
    "description": "the {{data.field_label}} input field"  → "the Vendor Number input field"
"""

from __future__ import annotations

import logging
import os
import re
import copy
from datetime import datetime
from typing import Any, Optional

from models.schemas import TestCase, TestStep, TestSuiteRequest
from data_provider.data_models import DataContext, DataRow

logger = logging.getLogger("qa.data_provider.resolver")

# Regex pattern for {{...}} templates
TEMPLATE_PATTERN = re.compile(r"\{\{(.+?)\}\}")


class TemplateResolver:
    """
    Resolves template placeholders in test case fields.

    Resolution order:
    1. {{data.xxx}}      → DataContext current row values
    2. {{row_index}}     → Current Excel row number
    3. {{iteration}}     → Current iteration counter
    4. {{env.XXX}}       → os.environ[XXX]
    5. {{now}}           → ISO timestamp
    6. {{now:format}}    → Formatted timestamp
    """

    def __init__(self, data_context: DataContext, default_sheet: Optional[str] = None):
        self.context = data_context
        self.default_sheet = default_sheet or self._first_sheet()
        self._iteration = 0

    def _first_sheet(self) -> str:
        """Get the first sheet name from context."""
        if self.context.sheets:
            return next(iter(self.context.sheets))
        return ""

    def resolve_suite(
        self,
        suite: TestSuiteRequest,
        iteration_rows: Optional[list[DataRow]] = None,
    ) -> list[TestSuiteRequest]:
        """
        Resolve a test suite into one or more suites, one per data row.

        If iteration_rows is provided, creates N copies of the suite,
        each with its data row injected. Otherwise, resolves once with
        the first available row.

        Returns:
            List of resolved TestSuiteRequest objects (deep copies).
        """
        if not iteration_rows:
            # No iteration — try to resolve with first row of default sheet
            rows = self.context.get_iteration_rows(self.default_sheet)
            if rows:
                iteration_rows = [rows[0]]
            else:
                logger.warning("No data rows available for template resolution")
                return [suite]

        resolved_suites: list[TestSuiteRequest] = []

        for idx, row in enumerate(iteration_rows, start=1):
            self._iteration = idx
            resolved_suite = self._resolve_suite_for_row(suite, row, idx)
            resolved_suites.append(resolved_suite)

        logger.info(
            f"Resolved {len(resolved_suites)} suite iterations "
            f"from sheet '{self.default_sheet}'"
        )
        return resolved_suites

    def _resolve_suite_for_row(
        self, suite: TestSuiteRequest, row: DataRow, iteration: int
    ) -> TestSuiteRequest:
        """Create a deep copy of the suite with all templates resolved for one row."""
        suite_dict = suite.model_dump()

        # Update suite metadata with row info
        suite_dict["suite_id"] = self._resolve_string(
            f"{suite.suite_id}_row{row.row_index}", row
        )
        suite_dict["suite_name"] = self._resolve_string(
            f"{suite.suite_name} [Row {row.row_index}]", row
        )

        # Resolve each test case
        for tc_dict in suite_dict.get("test_cases", []):
            tc_dict["test_id"] = self._resolve_string(
                f"{tc_dict['test_id']}_row{row.row_index}", row
            )
            tc_dict["name"] = self._resolve_string(tc_dict["name"], row)

            if tc_dict.get("description"):
                tc_dict["description"] = self._resolve_string(
                    tc_dict["description"], row
                )

            # Resolve each step
            for step_dict in tc_dict.get("steps", []):
                self._resolve_step_dict(step_dict, row)

        return TestSuiteRequest(**suite_dict)

    def _resolve_step_dict(self, step_dict: dict, row: DataRow):
        """Resolve all template fields in a step dictionary."""
        # Resolve step name
        step_dict["name"] = self._resolve_string(step_dict.get("name", ""), row)

        # Resolve target description
        target = step_dict.get("target")
        if target and target.get("description"):
            target["description"] = self._resolve_string(
                target["description"], row
            )
        if target and target.get("selector"):
            target["selector"] = self._resolve_string(
                target["selector"], row
            )

        # Resolve data value (the most common case)
        data = step_dict.get("data")
        if data and data.get("value") is not None:
            data["value"] = self._resolve_value(data["value"], row)

    def _resolve_value(self, value: Any, row: DataRow) -> Any:
        """
        Resolve a value — handles both string templates and pass-through.
        If value is a string containing {{...}}, resolve it.
        Otherwise, return as-is.
        """
        if not isinstance(value, str):
            return value

        if "{{" not in value:
            return value

        return self._resolve_string(value, row)

    def _resolve_string(self, template: str, row: DataRow) -> str:
        """
        Resolve all {{...}} placeholders in a string.

        Supports:
            {{data.vendor_number}}  → row.values["vendor_number"]
            {{data.PO_Data.vendor}} → specific sheet lookup
            {{row_index}}           → row.row_index
            {{iteration}}           → current iteration number
            {{env.SAP_USERNAME}}    → os.environ["SAP_USERNAME"]
            {{now}}                 → 2026-03-25T14:30:00
            {{now:%Y-%m-%d}}        → 2026-03-25
        """
        def replacer(match: re.Match) -> str:
            expr = match.group(1).strip()

            # {{data.xxx}} or {{data.sheet.xxx}}
            if expr.startswith("data."):
                return self._resolve_data_expr(expr[5:], row)

            # {{row_index}}
            if expr == "row_index":
                return str(row.row_index)

            # {{iteration}}
            if expr == "iteration":
                return str(self._iteration)

            # {{env.XXX}}
            if expr.startswith("env."):
                env_var = expr[4:]
                value = os.environ.get(env_var, "")
                if not value:
                    logger.warning(f"Environment variable not set: {env_var}")
                return value

            # {{now}} or {{now:format}}
            if expr.startswith("now"):
                if ":" in expr:
                    fmt = expr.split(":", 1)[1]
                    return datetime.now().strftime(fmt)
                return datetime.now().isoformat()

            # Unknown — leave as-is and warn
            logger.warning(f"Unresolved template: {{{{{expr}}}}}")
            return match.group(0)

        return TEMPLATE_PATTERN.sub(replacer, template)

    def _resolve_data_expr(self, expr: str, row: DataRow) -> str:
        """
        Resolve a data expression.
        - "vendor_number" → row.values["vendor_number"]
        - "PO_Data.vendor_number" → lookup in specific sheet
        """
        parts = expr.split(".", 1)

        if len(parts) == 1:
            # Simple: variable from current row
            var_name = parts[0]
            value = row.values.get(var_name)
            if value is None:
                logger.warning(
                    f"Variable '{var_name}' not found in row {row.row_index}. "
                    f"Available: {list(row.values.keys())}"
                )
                return ""
            return str(value)

        # Sheet-qualified: "SheetName.variable"
        sheet_name, var_name = parts
        sheet_rows = self.context.sheets.get(sheet_name, [])
        # Look in the same row_index if available
        for r in sheet_rows:
            if r.row_index == row.row_index:
                value = r.values.get(var_name)
                return str(value) if value is not None else ""

        logger.warning(
            f"Variable '{var_name}' not found in sheet '{sheet_name}' "
            f"for row {row.row_index}"
        )
        return ""


def resolve_test_data(
    suite: TestSuiteRequest,
    data_context: DataContext,
    iteration_sheet: Optional[str] = None,
) -> list[TestSuiteRequest]:
    """
    Convenience function: resolve a suite with data from a DataContext.

    Returns a list of resolved suites (one per iteration row).
    """
    sheet = iteration_sheet or (
        next(iter(data_context.sheets)) if data_context.sheets else ""
    )

    resolver = TemplateResolver(data_context, default_sheet=sheet)
    iteration_rows = data_context.get_iteration_rows(sheet)

    return resolver.resolve_suite(suite, iteration_rows)
