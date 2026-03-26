"""
Data source models for the Data Provider system.
Defines configuration for Excel files, SharePoint connections,
template syntax, and data mapping rules.

These models extend the framework WITHOUT modifying existing schemas.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field, field_validator


# ─── Data Source Types ───────────────────────────────────────────────────────

class DataSourceType(str, Enum):
    """Supported data source types."""
    EXCEL_LOCAL = "excel_local"           # Local .xlsx file
    EXCEL_SHAREPOINT = "excel_sharepoint" # Excel from SharePoint
    CSV_LOCAL = "csv_local"               # Local CSV file
    JSON_INLINE = "json_inline"           # Inline JSON data
    API_REST = "api_rest"                 # External REST API


class IterationMode(str, Enum):
    """How to iterate over data rows."""
    SINGLE_ROW = "single_row"   # Use a specific row
    ALL_ROWS = "all_rows"       # Execute test once per row
    ROW_RANGE = "row_range"     # Execute for a range of rows
    FILTERED = "filtered"       # Execute for rows matching a filter


# ─── SharePoint Configuration ────────────────────────────────────────────────

class SharePointConfig(BaseModel):
    """
    Configuration for downloading files from SharePoint.
    Supports both SharePoint Online (Microsoft 365) and On-Premise.
    """
    site_url: str = Field(
        ...,
        description="SharePoint site URL. Example: 'https://company.sharepoint.com/sites/QATeam'"
    )
    file_path: str = Field(
        ...,
        description="Relative path to the file within the site. "
                    "Example: 'Shared Documents/Reports/TestData.xlsx'"
    )
    auth_method: str = Field(
        "client_credentials",
        description="Authentication method: 'client_credentials' | 'username_password' | 'token'"
    )
    tenant_id: Optional[str] = Field(
        None,
        description="Azure AD tenant ID (for client_credentials)"
    )
    client_id: Optional[str] = Field(
        None,
        description="Azure AD app client ID"
    )
    client_secret: Optional[str] = Field(
        None,
        description="Azure AD app client secret (use env var in practice)"
    )
    username: Optional[str] = Field(None, description="For username_password auth")
    password: Optional[str] = Field(None, description="For username_password auth")
    access_token: Optional[str] = Field(None, description="Pre-obtained bearer token")


# ─── Excel Mapping Configuration ─────────────────────────────────────────────

class ColumnMapping(BaseModel):
    """
    Maps an Excel column to a logical variable name used in templates.

    Example:
        column: "B"
        variable_name: "vendor_number"
        → In step data: "{{data.vendor_number}}" resolves to cell B{row}
    """
    column: str = Field(
        ...,
        description="Excel column letter(s). Example: 'B', 'AA'"
    )
    variable_name: str = Field(
        ...,
        description="Logical name for use in templates. Example: 'vendor_number'"
    )
    data_type: str = Field(
        "string",
        description="Expected type: 'string' | 'number' | 'date' | 'boolean'"
    )
    required: bool = Field(
        True,
        description="If True, empty cells cause a validation error"
    )
    default_value: Optional[str] = Field(
        None,
        description="Default value if cell is empty and required=False"
    )
    date_format: Optional[str] = Field(
        None,
        description="Date format for parsing. Example: '%Y-%m-%d', '%m/%d/%Y'"
    )


class ExcelSheetConfig(BaseModel):
    """Configuration for a single sheet within an Excel workbook."""
    sheet_name: str = Field(
        ...,
        description="Sheet name. Example: 'Sheet1', 'PO_Data', 'Vendors'"
    )
    header_row: int = Field(
        1,
        description="Row number that contains column headers (1-based)"
    )
    data_start_row: int = Field(
        2,
        description="First row of actual data (1-based)"
    )
    column_mappings: list[ColumnMapping] = Field(
        default_factory=list,
        description="Maps columns to variable names. If empty, uses header names."
    )

    @field_validator("header_row", "data_start_row")
    @classmethod
    def must_be_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("Row numbers must be >= 1")
        return v


class RowFilter(BaseModel):
    """Filter condition for selecting specific rows from Excel."""
    column: str = Field(..., description="Column letter or variable name")
    operator: str = Field(
        "equals",
        description="Comparison: 'equals' | 'contains' | 'starts_with' | 'not_empty' | 'gt' | 'lt'"
    )
    value: Optional[str] = Field(None, description="Value to compare against")


# ─── Excel & Iteration Config (defined before DataSourceConfig) ──────────────

class ExcelConfig(BaseModel):
    """Top-level Excel workbook configuration."""
    sheets: list[ExcelSheetConfig] = Field(
        ...,
        min_length=1,
        description="Configuration for each sheet to read"
    )
    password: Optional[str] = Field(
        None,
        description="Workbook password if protected"
    )


class IterationConfig(BaseModel):
    """Controls how the framework iterates over data rows."""
    mode: IterationMode = Field(
        IterationMode.ALL_ROWS,
        description="Iteration strategy"
    )
    sheet_name: str = Field(
        ...,
        description="Which sheet to iterate over"
    )
    specific_row: Optional[int] = Field(
        None,
        description="For SINGLE_ROW mode: which row (1-based data row, not Excel row)"
    )
    row_start: Optional[int] = Field(
        None,
        description="For ROW_RANGE mode: start data row (inclusive)"
    )
    row_end: Optional[int] = Field(
        None,
        description="For ROW_RANGE mode: end data row (inclusive)"
    )
    filters: list[RowFilter] = Field(
        default_factory=list,
        description="For FILTERED mode: conditions that rows must match"
    )
    max_rows: Optional[int] = Field(
        None,
        description="Safety limit: max rows to process (prevents runaway execution)"
    )


# ─── Data Source Definition ──────────────────────────────────────────────────

class DataSourceConfig(BaseModel):
    """
    Complete data source definition.
    Attached to a TestSuiteRequest to provide external data.
    """
    source_id: str = Field(
        ...,
        description="Unique identifier for this data source. Example: 'po_data'"
    )
    source_type: DataSourceType = Field(
        ...,
        description="Where data comes from"
    )

    # File source (local)
    file_path: Optional[str] = Field(
        None,
        description="Local file path for excel_local or csv_local"
    )

    # SharePoint source
    sharepoint: Optional[SharePointConfig] = Field(
        None,
        description="SharePoint configuration (required for excel_sharepoint)"
    )

    # Excel parsing config
    excel: Optional[ExcelConfig] = Field(
        None,
        description="Excel workbook configuration"
    )

    # Iteration settings
    iteration: Optional[IterationConfig] = Field(
        None,
        description="How to iterate over data rows"
    )

    # Cache settings
    cache_downloaded_file: bool = Field(
        True,
        description="Keep downloaded file locally for debugging/re-runs"
    )
    cache_dir: str = Field(
        "data_provider/cache",
        description="Directory to cache downloaded files"
    )


# ─── Resolved Data Context ──────────────────────────────────────────────────

class DataRow(BaseModel):
    """A single resolved data row ready for template injection."""
    row_index: int = Field(..., description="Original Excel row number (1-based)")
    values: dict[str, Any] = Field(
        default_factory=dict,
        description="Variable name → resolved value"
    )
    sheet_name: str = Field(..., description="Source sheet")


class DataContext(BaseModel):
    """
    The complete resolved dataset — result of the Data Provider.
    This is what gets injected into test steps before execution.
    """
    source_id: str
    source_file: str = Field(..., description="Path to the actual file used (local or cached)")
    loaded_at: str = Field(..., description="ISO timestamp of when data was loaded")
    sheets: dict[str, list[DataRow]] = Field(
        default_factory=dict,
        description="Sheet name → list of data rows"
    )
    total_rows: int = 0
    validation_errors: list[str] = Field(default_factory=list)

    def get_row(self, sheet: str, row_index: int) -> Optional[DataRow]:
        """Get a specific data row by sheet and index."""
        rows = self.sheets.get(sheet, [])
        for r in rows:
            if r.row_index == row_index:
                return r
        return None

    def get_iteration_rows(self, sheet: str) -> list[DataRow]:
        """Get all rows for iteration from a sheet."""
        return self.sheets.get(sheet, [])

    def flat_values(self, sheet: str, row_index: int) -> dict[str, Any]:
        """Get flat dict of variable_name → value for template resolution."""
        row = self.get_row(sheet, row_index)
        return row.values if row else {}
