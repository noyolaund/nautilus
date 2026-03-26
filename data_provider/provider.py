"""
Data Provider — Main orchestrator for the data loading pipeline.

Pipeline:
    1. DOWNLOAD  — Get file from SharePoint (or use local path)
    2. PARSE     — Read Excel with openpyxl, build DataContext
    3. VALIDATE  — Check required fields, types, row counts (fail-fast)
    4. RESOLVE   — Inject data into test step templates
    5. EXPAND    — Generate N suite copies for N data rows (data-driven)

Usage:
    provider = DataProvider(data_source_config)
    context = await provider.load()           # Steps 1-3
    suites = provider.resolve(suite, context)  # Steps 4-5

Or as a one-liner:
    suites = await DataProvider.load_and_resolve(data_config, suite)
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from models.schemas import TestSuiteRequest
from data_provider.data_models import (
    DataSourceConfig,
    DataSourceType,
    DataContext,
)
from data_provider.excel_parser import ExcelParser
from data_provider.template_resolver import TemplateResolver

logger = logging.getLogger("qa.data_provider")


class DataValidationError(Exception):
    """Raised when data validation fails and execution should not proceed."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        msg = f"Data validation failed with {len(errors)} error(s):\n"
        msg += "\n".join(f"  • {e}" for e in errors[:10])
        if len(errors) > 10:
            msg += f"\n  ... and {len(errors) - 10} more"
        super().__init__(msg)


class DataProvider:
    """
    Orchestrates the full data loading pipeline.

    This class does NOT modify any existing framework files.
    It produces resolved TestSuiteRequest objects that the existing
    engines can execute without knowing about Excel or SharePoint.
    """

    def __init__(self, config: DataSourceConfig):
        self.config = config
        self._context: Optional[DataContext] = None

    async def load(self) -> DataContext:
        """
        Execute steps 1-3: Download → Parse → Validate.

        Returns:
            DataContext with all rows loaded and validated.

        Raises:
            DataValidationError: If required fields are missing.
            FileNotFoundError: If file doesn't exist.
            PermissionError: If SharePoint auth fails.
        """
        logger.info(f"Loading data source: {self.config.source_id} ({self.config.source_type.value})")

        # ── Step 1: Get the file ─────────────────────────────────────
        file_path = await self._acquire_file()
        logger.info(f"  File acquired: {file_path}")

        # ── Step 2: Parse ────────────────────────────────────────────
        parser = ExcelParser(file_path, self.config)
        context = parser.parse()
        logger.info(
            f"  Parsed: {context.total_rows} rows across "
            f"{len(context.sheets)} sheet(s)"
        )

        # ── Step 3: Validate ─────────────────────────────────────────
        self._validate(context)

        self._context = context
        return context

    def resolve(
        self,
        suite: TestSuiteRequest,
        context: Optional[DataContext] = None,
    ) -> list[TestSuiteRequest]:
        """
        Execute steps 4-5: Resolve templates → Expand iterations.

        Args:
            suite: The original TestSuiteRequest with {{data.xxx}} templates.
            context: DataContext to use (or uses the one from load()).

        Returns:
            List of resolved suites — one per data row for data-driven execution.
        """
        ctx = context or self._context
        if not ctx:
            raise RuntimeError("Call load() before resolve(), or pass a DataContext.")

        # Determine iteration sheet
        iteration_sheet = None
        if self.config.iteration:
            iteration_sheet = self.config.iteration.sheet_name

        resolver = TemplateResolver(ctx, default_sheet=iteration_sheet)
        iteration_rows = ctx.get_iteration_rows(
            iteration_sheet or next(iter(ctx.sheets), "")
        )

        if not iteration_rows:
            logger.warning("No data rows for iteration — returning original suite")
            return [suite]

        resolved = resolver.resolve_suite(suite, iteration_rows)

        logger.info(
            f"  Resolved: {len(resolved)} suite iteration(s) from "
            f"{len(iteration_rows)} data rows"
        )

        return resolved

    @classmethod
    async def load_and_resolve(
        cls,
        config: DataSourceConfig,
        suite: TestSuiteRequest,
    ) -> list[TestSuiteRequest]:
        """
        Convenience: load data and resolve in one call.

        Usage:
            suites = await DataProvider.load_and_resolve(data_config, original_suite)
            for s in suites:
                engine = HybridPlaywrightEngine(s)
                result = await engine.execute_suite()
        """
        provider = cls(config)
        await provider.load()
        return provider.resolve(suite)

    async def _acquire_file(self) -> str:
        """Get the file — download from SharePoint or use local path."""

        if self.config.source_type == DataSourceType.EXCEL_SHAREPOINT:
            return await self._download_from_sharepoint()

        elif self.config.source_type in (DataSourceType.EXCEL_LOCAL, DataSourceType.CSV_LOCAL):
            path = self.config.file_path
            if not path:
                raise ValueError(
                    f"file_path is required for source_type={self.config.source_type.value}"
                )
            if not Path(path).exists():
                raise FileNotFoundError(f"Local file not found: {path}")
            return path

        else:
            raise ValueError(
                f"Unsupported source_type for file acquisition: {self.config.source_type.value}"
            )

    async def _download_from_sharepoint(self) -> str:
        """Download file from SharePoint using the connector."""
        if not self.config.sharepoint:
            raise ValueError(
                "sharepoint config is required for source_type=excel_sharepoint"
            )

        from data_provider.connectors.sharepoint import SharePointConnector

        connector = SharePointConnector(
            self.config.sharepoint,
            cache_dir=self.config.cache_dir,
        )
        return await connector.download()

    def _validate(self, context: DataContext):
        """
        Validate the data context — fail fast if critical errors exist.

        Checks:
        - At least one data row exists
        - Required fields have values
        - No type coercion failures on required fields
        """
        critical_errors = []

        if context.total_rows == 0:
            critical_errors.append(
                "No data rows found. Check sheet_name, data_start_row, and "
                "that the Excel file contains data."
            )

        # Filter for truly critical errors (required fields)
        for error in context.validation_errors:
            if "Required field" in error:
                critical_errors.append(error)

        if critical_errors:
            logger.error(
                f"Data validation FAILED — {len(critical_errors)} critical error(s). "
                f"Execution will NOT proceed."
            )
            raise DataValidationError(critical_errors)

        # Log non-critical warnings
        warnings = [
            e for e in context.validation_errors
            if e not in critical_errors
        ]
        if warnings:
            for w in warnings[:5]:
                logger.warning(f"  Data warning: {w}")

        logger.info(
            f"  Validation PASSED: {context.total_rows} rows, "
            f"{len(critical_errors)} errors, {len(warnings)} warnings"
        )

    def get_data_summary(self) -> dict:
        """Return a summary of loaded data for reports."""
        if not self._context:
            return {"status": "not_loaded"}

        return {
            "source_id": self._context.source_id,
            "source_file": self._context.source_file,
            "loaded_at": self._context.loaded_at,
            "total_rows": self._context.total_rows,
            "sheets": {
                name: len(rows)
                for name, rows in self._context.sheets.items()
            },
            "validation_errors": len(self._context.validation_errors),
        }
