from __future__ import annotations

from acp.schema import EnvVariable, HttpHeader, McpServerHttp, McpServerSse, McpServerStdio

from talk2agent.config import NameValueConfig, WorkspaceConfig


def build_workspace_mcp_servers(workspace: WorkspaceConfig):
    servers = []
    for server in workspace.mcp_servers:
        if server.transport == "stdio":
            servers.append(
                McpServerStdio(
                    name=server.name,
                    command=server.command or "",
                    args=list(server.args),
                    env=_build_name_value_items(server.env, EnvVariable),
                )
            )
            continue
        if server.transport == "http":
            servers.append(
                McpServerHttp(
                    name=server.name,
                    url=server.url or "",
                    headers=_build_name_value_items(server.headers, HttpHeader),
                )
            )
            continue
        if server.transport == "sse":
            servers.append(
                McpServerSse(
                    name=server.name,
                    url=server.url or "",
                    headers=_build_name_value_items(server.headers, HttpHeader),
                )
            )
            continue
        raise ValueError(f"unsupported MCP transport: {server.transport}")
    return servers


def _build_name_value_items(entries: list[NameValueConfig], model):
    return [model(name=entry.name, value=entry.value) for entry in entries]
