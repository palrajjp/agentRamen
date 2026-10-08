"""OIDC-protected, read-only Streamable HTTP MCP over a mounted snapshot."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import jwt
from jwt import PyJWKClient
from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from . import __version__
from .core import (
    AgentRamenError,
    architecture,
    context_for,
    dependency_impact,
    explain_file,
    find_dependencies,
    graph_export,
    hotspots,
    repo_search,
    repository_status,
)
from .memory import search_shared_memories


class OIDCVerifier(TokenVerifier):
    """Verify company-issued JWT access tokens against a configured JWKS endpoint."""

    def __init__(
        self,
        *,
        issuer: str,
        jwks_url: str,
        audience: str,
        resource_url: str,
        required_scope: str,
        allowed_group: str | None = None,
        group_claim: str = "groups",
    ) -> None:
        if not issuer.startswith("https://") or not jwks_url.startswith("https://"):
            raise AgentRamenError("OIDC issuer and JWKS URL must use HTTPS.")
        if not audience or not required_scope:
            raise AgentRamenError("OIDC audience and required scope are required.")
        self.issuer = issuer
        self.audience = audience
        self.resource_url = resource_url
        self.required_scope = required_scope
        self.allowed_group = allowed_group
        self.group_claim = group_claim
        self.jwks = PyJWKClient(jwks_url, cache_jwk_set=True)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            signing_key = await asyncio.to_thread(
                self.jwks.get_signing_key_from_jwt, token
            )
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "sub"]},
                leeway=60,
            )
        except (jwt.PyJWTError, jwt.PyJWKClientError, ValueError, TypeError):
            return None

        raw_scopes = claims.get("scope", claims.get("scp", ""))
        if isinstance(raw_scopes, str):
            scopes = raw_scopes.split()
        elif isinstance(raw_scopes, list) and all(isinstance(item, str) for item in raw_scopes):
            scopes = raw_scopes
        else:
            return None
        if self.required_scope not in scopes:
            return None

        if self.allowed_group:
            groups = claims.get(self.group_claim, [])
            if isinstance(groups, str):
                groups = [groups]
            if not isinstance(groups, list) or self.allowed_group not in groups:
                return None

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            return None
        return AccessToken(
            token=token,
            client_id=str(claims.get("azp", claims.get("client_id", subject))),
            scopes=scopes,
            expires_at=int(claims["exp"]),
            resource=self.resource_url,
            subject=subject,
            claims={"repository_access": "read"},
        )


def create_remote_server(
    snapshot_root: Path,
    *,
    repository_id: str,
    snapshot_commit: str,
    issuer: str,
    jwks_url: str,
    audience: str,
    resource_url: str,
    allowed_hosts: list[str],
    required_scope: str = "agentramen:read",
    allowed_group: str | None = None,
    group_claim: str = "groups",
    allowed_origins: list[str] | None = None,
) -> MCPServer:
    """Build a single-repository, read-only remote MCP server."""
    if not allowed_hosts:
        raise AgentRamenError("At least one public Host header must be allowlisted.")
    snapshot_root = snapshot_root.resolve()
    auth = AuthSettings(
        issuer_url=AnyHttpUrl(issuer),
        resource_server_url=AnyHttpUrl(resource_url),
        required_scopes=[required_scope],
        validate_token_resource=True,
    )
    verifier = OIDCVerifier(
        issuer=issuer,
        jwks_url=jwks_url,
        audience=audience,
        resource_url=resource_url,
        required_scope=required_scope,
        allowed_group=allowed_group,
        group_claim=group_claim,
    )
    server = MCPServer(
        "AgentRamen Central",
        version=__version__,
        instructions=(
            f"Read-only repository context for {repository_id} at a pinned Git snapshot. "
            "Use repo_context for task-scoped files before broad source reads. "
            "This endpoint cannot write source or memory data."
        ),
        token_verifier=verifier,
        auth=auth,
    )

    @server.tool(description="Show the pinned repository snapshot revision and index counts.")
    def repo_status() -> dict[str, object]:
        return {
            "repository_id": repository_id,
            "revision": snapshot_commit,
            **repository_status(snapshot_root),
        }

    @server.tool(description="Retrieve bounded repository context for a coding task.")
    def repo_context(task: str, token_budget: int = 2000) -> dict[str, Any]:
        return context_for(snapshot_root, task, token_budget)

    @server.tool(description="Search indexed file paths and symbols in the pinned snapshot.")
    def repo_search(query: str, limit: int = 20) -> list[dict[str, object]]:
        return _repo_search(snapshot_root, query, limit)

    @server.tool(description="Summarize repository languages, modules, and graph relationships.")
    def repo_architecture() -> dict[str, object]:
        return architecture(snapshot_root)

    @server.tool(description="Explain a file's symbols, dependencies, and dependents.")
    def repo_explain(path: str) -> dict[str, object]:
        return explain_file(snapshot_root, path)

    @server.tool(description="Show direct/indirect impact for a target file.")
    def repo_impact(path: str) -> dict[str, object]:
        return dependency_impact(snapshot_root, path)

    @server.tool(description="List direct dependencies for a target file.")
    def repo_dependencies(path: str) -> list[str]:
        return find_dependencies(snapshot_root, path)

    @server.tool(description="List files with the most indexed Git changes.")
    def repo_hotspots(limit: int = 20) -> list[dict[str, object]]:
        return hotspots(snapshot_root, limit)

    @server.tool(description="Search approved team memories in this pinned snapshot.")
    def repo_team_memory_search(query: str, limit: int = 10) -> list[dict[str, object]]:
        return search_shared_memories(snapshot_root, query, limit)

    @server.tool(description="Export graph nodes and relationships for this snapshot.")
    def repo_graph() -> dict[str, object]:
        return graph_export(snapshot_root)

    return server


def _repo_search(root: Path, query: str, limit: int) -> list[dict[str, object]]:
    return repo_search(root, query, limit)


def run_remote_server(
    snapshot_root: Path,
    *,
    repository_id: str,
    snapshot_commit: str,
    issuer: str,
    jwks_url: str,
    audience: str,
    resource_url: str,
    allowed_hosts: list[str],
    required_scope: str = "agentramen:read",
    allowed_group: str | None = None,
    group_claim: str = "groups",
    allowed_origins: list[str] | None = None,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    server = create_remote_server(
        snapshot_root,
        repository_id=repository_id,
        snapshot_commit=snapshot_commit,
        issuer=issuer,
        jwks_url=jwks_url,
        audience=audience,
        resource_url=resource_url,
        allowed_hosts=allowed_hosts,
        required_scope=required_scope,
        allowed_group=allowed_group,
        group_claim=group_claim,
        allowed_origins=allowed_origins,
    )
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins or [],
    )
    server.run(
        transport="streamable-http",
        host=host,
        port=port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=transport_security,
    )
