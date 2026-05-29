import json

from gateway.mcp.manager import MCPTool, MCPToolResult


def mcp_to_anthropic(tools: list[MCPTool]) -> list[dict]:
    return [
        {
            "name": tool.registered_name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }
        for tool in tools
    ]


def mcp_to_openrouter(tools: list[MCPTool]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.registered_name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }
        for tool in tools
    ]


def anthropic_tool_use_to_mcp(block: dict) -> tuple[str, dict]:
    """Extract (tool_name, arguments) from a normalised Anthropic tool_use block."""
    return block["name"], block["input"]


def openrouter_tool_call_to_mcp(block: dict) -> tuple[str, dict]:
    """Extract (tool_name, arguments) from a normalised OpenRouter tool_use block.

    Both providers normalise to the same dict shape so this mirrors the Anthropic extractor.
    """
    return block["name"], block["input"]


def mcp_result_to_anthropic(tool_use_id: str, result: MCPToolResult) -> dict:
    if result.is_error:
        content = [{"type": "text", "text": _extract_text(result.content)}]
    else:
        content = result.content
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
        "is_error": result.is_error,
    }


def mcp_result_to_openrouter(tool_call_id: str, result: MCPToolResult) -> dict:
    if result.is_error:
        content = _extract_text(result.content)
    else:
        content = json.dumps(result.content)
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": content,
    }


# Ollama and Google use the same OpenAI-compat format as OpenRouter
mcp_to_ollama = mcp_to_openrouter
mcp_to_ollama_cloud = mcp_to_openrouter
mcp_to_google = mcp_to_openrouter
ollama_tool_call_to_mcp = openrouter_tool_call_to_mcp
ollama_cloud_tool_call_to_mcp = openrouter_tool_call_to_mcp
google_tool_call_to_mcp = openrouter_tool_call_to_mcp
mcp_result_to_ollama = mcp_result_to_openrouter
mcp_result_to_ollama_cloud = mcp_result_to_openrouter
mcp_result_to_google = mcp_result_to_openrouter


def _extract_text(content: list[dict]) -> str:
    parts = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(parts) if parts else str(content)
