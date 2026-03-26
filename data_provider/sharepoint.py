"""
SharePoint Connector — Downloads files from SharePoint Online.

Uses Microsoft Graph API for SharePoint Online (Microsoft 365).
Supports client_credentials, username/password, and pre-obtained token auth.

Usage:
    connector = SharePointConnector(config)
    local_path = await connector.download()
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import httpx

from data_provider.data_models import SharePointConfig

logger = logging.getLogger("qa.data_provider.sharepoint")


class SharePointConnector:
    """
    Downloads files from SharePoint Online via Microsoft Graph API.

    Authentication flow (client_credentials):
    1. POST to Azure AD token endpoint with client_id + client_secret
    2. Receive bearer token
    3. GET file content via /sites/{site-id}/drive/root:/{path}:/content
    """

    GRAPH_BASE = "https://graph.microsoft.com/v1.0"
    TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

    def __init__(self, config: SharePointConfig, cache_dir: str = "data_provider/cache"):
        self.config = config
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._token: Optional[str] = None

    async def download(self) -> str:
        """
        Download the file from SharePoint and return the local path.

        Returns:
            str: Local file path where the downloaded file is saved.

        Raises:
            ConnectionError: If SharePoint is unreachable.
            PermissionError: If authentication fails.
            FileNotFoundError: If the file doesn't exist in SharePoint.
        """
        token = await self._get_token()
        file_bytes = await self._download_file(token)

        # Save to cache
        filename = Path(self.config.file_path).name
        local_path = self.cache_dir / filename
        local_path.write_bytes(file_bytes)

        logger.info(
            f"Downloaded from SharePoint: {self.config.file_path} "
            f"→ {local_path} ({len(file_bytes):,} bytes)"
        )
        return str(local_path)

    async def _get_token(self) -> str:
        """Obtain an access token based on the configured auth method."""

        # Pre-obtained token
        if self.config.auth_method == "token" and self.config.access_token:
            return self.config.access_token

        # Client credentials flow (service-to-service, no user context)
        if self.config.auth_method == "client_credentials":
            return await self._client_credentials_flow()

        # Username/password flow (delegated, requires user context)
        if self.config.auth_method == "username_password":
            return await self._password_flow()

        raise ValueError(f"Unsupported auth method: {self.config.auth_method}")

    async def _client_credentials_flow(self) -> str:
        """Azure AD client credentials grant."""
        tenant_id = self.config.tenant_id or os.getenv("SHAREPOINT_TENANT_ID", "")
        client_id = self.config.client_id or os.getenv("SHAREPOINT_CLIENT_ID", "")
        client_secret = self.config.client_secret or os.getenv("SHAREPOINT_CLIENT_SECRET", "")

        if not all([tenant_id, client_id, client_secret]):
            raise PermissionError(
                "SharePoint client_credentials requires tenant_id, client_id, "
                "and client_secret. Set them in the config or as env vars: "
                "SHAREPOINT_TENANT_ID, SHAREPOINT_CLIENT_ID, SHAREPOINT_CLIENT_SECRET"
            )

        token_url = self.TOKEN_URL_TEMPLATE.format(tenant_id=tenant_id)

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": "https://graph.microsoft.com/.default",
                },
            )

            if response.status_code != 200:
                logger.error(f"SharePoint auth failed: {response.status_code} {response.text}")
                raise PermissionError(
                    f"SharePoint authentication failed (HTTP {response.status_code}). "
                    f"Verify your tenant_id, client_id, and client_secret."
                )

            data = response.json()
            self._token = data["access_token"]
            logger.debug("SharePoint access token obtained via client_credentials")
            return self._token

    async def _password_flow(self) -> str:
        """Azure AD resource owner password grant (delegated)."""
        tenant_id = self.config.tenant_id or os.getenv("SHAREPOINT_TENANT_ID", "")
        client_id = self.config.client_id or os.getenv("SHAREPOINT_CLIENT_ID", "")
        username = self.config.username or os.getenv("SHAREPOINT_USERNAME", "")
        password = self.config.password or os.getenv("SHAREPOINT_PASSWORD", "")

        token_url = self.TOKEN_URL_TEMPLATE.format(tenant_id=tenant_id)

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                token_url,
                data={
                    "grant_type": "password",
                    "client_id": client_id,
                    "username": username,
                    "password": password,
                    "scope": "https://graph.microsoft.com/.default",
                },
            )

            if response.status_code != 200:
                raise PermissionError(
                    f"SharePoint password auth failed (HTTP {response.status_code})"
                )

            data = response.json()
            self._token = data["access_token"]
            return self._token

    async def _download_file(self, token: str) -> bytes:
        """Download file content from SharePoint via Graph API."""

        # Extract hostname from site_url for Graph API
        # e.g., "https://company.sharepoint.com/sites/QATeam"
        # → hostname: "company.sharepoint.com", site_path: "/sites/QATeam"
        from urllib.parse import urlparse
        parsed = urlparse(self.config.site_url)
        hostname = parsed.hostname
        site_path = parsed.path.rstrip("/")

        # Graph API endpoint for file content
        # GET /sites/{hostname}:/{site-path}:/drive/root:/{file-path}:/content
        file_path = self.config.file_path.lstrip("/")
        url = (
            f"{self.GRAPH_BASE}/sites/{hostname}:{site_path}:"
            f"/drive/root:/{file_path}:/content"
        )

        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
            )

            if response.status_code == 404:
                raise FileNotFoundError(
                    f"File not found in SharePoint: {self.config.file_path}. "
                    f"Verify the file_path is relative to the document library root."
                )
            elif response.status_code == 403:
                raise PermissionError(
                    f"Access denied to {self.config.file_path}. "
                    f"Ensure the app has Files.Read.All or Sites.Read.All permissions."
                )
            elif response.status_code != 200:
                raise ConnectionError(
                    f"SharePoint download failed (HTTP {response.status_code}): "
                    f"{response.text[:200]}"
                )

            return response.content
