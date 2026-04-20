"""Agoragentic marketplace integration.

Route tasks to the Agoragentic capability marketplace for execution by
trusted autonomous agents. Enables cross-agent task routing and settlement.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# Load API key from secure credentials file
_CREDENTIALS_FILE = Path.home() / ".llm-router" / "agoragentic.json"
_API_KEY: Optional[str] = None
_AGENT_ID: Optional[str] = None


def _load_credentials() -> tuple[Optional[str], Optional[str]]:
    """Load Agoragentic credentials from secure storage."""
    global _API_KEY, _AGENT_ID
    
    if _API_KEY is not None:
        return _API_KEY, _AGENT_ID
    
    if not _CREDENTIALS_FILE.exists():
        return None, None
    
    try:
        creds = json.loads(_CREDENTIALS_FILE.read_text())
        _API_KEY = creds.get("api_key")
        _AGENT_ID = creds.get("id")
        return _API_KEY, _AGENT_ID
    except Exception as e:
        logger.error(f"Failed to load Agoragentic credentials: {e}")
        return None, None


async def agoragentic_execute(
    task: str,
    input_data: dict[str, Any],
    max_budget_usdc: Optional[float] = None,
) -> dict[str, Any]:
    """Execute a task on the Agoragentic marketplace.
    
    Routes the task to the best-matching trusted provider automatically.
    Handles wallet settlement via USDC on Base L2.
    
    Args:
        task: Task name (e.g., "code_review", "summarization", "web_scrape")
        input_data: Task input matching the provider's input schema
        max_budget_usdc: Maximum USDC to spend (optional, defaults to no limit)
    
    Returns:
        Task execution result with provider info and output
    
    Raises:
        ValueError: If Agoragentic is not configured (no API key found)
        httpx.HTTPError: If API call fails
    """
    api_key, agent_id = _load_credentials()
    
    if not api_key:
        raise ValueError(
            "Agoragentic not configured. Register first: "
            "python3 scripts/agoragentic_register.py"
        )
    
    async with httpx.AsyncClient() as client:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        
        payload = {
            "task": task,
            "input": input_data,
        }
        
        if max_budget_usdc is not None:
            payload["max_budget_usdc"] = max_budget_usdc
        
        response = await client.post(
            "https://agoragentic.com/api/execute",
            json=payload,
            headers=headers,
            timeout=300.0,  # 5 min timeout for long-running tasks
        )
        
        response.raise_for_status()
        result = response.json()
        
        logger.info(
            f"Task '{task}' executed via Agoragentic. "
            f"Provider: {result.get('provider')}, "
            f"Cost: ${result.get('cost_usdc', 0):.4f}"
        )
        
        return result


async def agoragentic_browse_capabilities(
    category: Optional[str] = None,
    trusted_only: bool = True,
) -> list[dict[str, Any]]:
    """Browse available capabilities on the Agoragentic marketplace.
    
    Args:
        category: Filter by category (e.g., "code-review", "summarization")
        trusted_only: Only show trust-verified providers
    
    Returns:
        List of available capabilities/providers
    """
    api_key, _ = _load_credentials()
    
    if not api_key:
        raise ValueError("Agoragentic not configured")
    
    async with httpx.AsyncClient() as client:
        headers = {"Authorization": f"Bearer {api_key}"}
        
        params = {}
        if category:
            params["category"] = category
        if trusted_only:
            params["trusted"] = "true"
        
        response = await client.get(
            "https://agoragentic.com/api/capabilities",
            headers=headers,
            params=params,
            timeout=30.0,
        )
        
        response.raise_for_status()
        return response.json().get("capabilities", [])


async def agoragentic_get_wallet() -> dict[str, Any]:
    """Get current wallet balance and status.
    
    Returns:
        Wallet info including balance, currency, and chain
    """
    api_key, _ = _load_credentials()
    
    if not api_key:
        raise ValueError("Agoragentic not configured")
    
    async with httpx.AsyncClient() as client:
        headers = {"Authorization": f"Bearer {api_key}"}
        
        response = await client.get(
            "https://agoragentic.com/api/wallet",
            headers=headers,
            timeout=30.0,
        )
        
        response.raise_for_status()
        return response.json()


async def agoragentic_get_agent_status() -> dict[str, Any]:
    """Get current agent status on Agoragentic.
    
    Returns:
        Agent registration status, seller activation, listings, etc.
    """
    api_key, _ = _load_credentials()
    
    if not api_key:
        raise ValueError("Agoragentic not configured")
    
    async with httpx.AsyncClient() as client:
        headers = {"Authorization": f"Bearer {api_key}"}
        
        response = await client.get(
            "https://agoragentic.com/api/agents/me",
            headers=headers,
            timeout=30.0,
        )
        
        response.raise_for_status()
        return response.json()


def register(mcp):
    """Register Agoragentic tools with MCP server."""
    
    @mcp.tool()
    async def agoragentic_task(
        task: str,
        input_json: str,
        max_budget_usdc: Optional[float] = None,
    ) -> str:
        """Execute a task on the Agoragentic capability marketplace.
        
        Routes automatically to the best-matching trusted provider.
        Handles USDC settlement on Base L2 blockchain.
        
        Args:
            task: Task type (e.g., "code_review", "summarization")
            input_json: Task input as JSON string
            max_budget_usdc: Maximum spend limit (optional)
        
        Returns:
            Execution result as JSON string
        """
        try:
            input_data = json.loads(input_json)
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"Invalid input JSON: {e}"})
        
        try:
            result = await agoragentic_execute(
                task=task,
                input_data=input_data,
                max_budget_usdc=max_budget_usdc,
            )
            return json.dumps(result)
        except Exception as e:
            logger.error(f"Agoragentic task failed: {e}")
            return json.dumps({"error": str(e)})
    
    @mcp.tool()
    async def agoragentic_browse() -> str:
        """Browse available services on the Agoragentic marketplace.
        
        Shows trust-verified providers and their capabilities.
        
        Returns:
            JSON list of available capabilities
        """
        try:
            capabilities = await agoragentic_browse_capabilities()
            return json.dumps({
                "capabilities": capabilities,
                "count": len(capabilities),
            })
        except Exception as e:
            logger.error(f"Failed to browse Agoragentic capabilities: {e}")
            return json.dumps({"error": str(e)})
    
    @mcp.tool()
    async def agoragentic_wallet() -> str:
        """Check Agoragentic wallet balance and status.
        
        Returns:
            Wallet info including balance, chain, and currency
        """
        try:
            wallet = await agoragentic_get_wallet()
            return json.dumps(wallet)
        except Exception as e:
            logger.error(f"Failed to get wallet status: {e}")
            return json.dumps({"error": str(e)})
    
    @mcp.tool()
    async def agoragentic_status() -> str:
        """Get llm-router agent status on Agoragentic.
        
        Shows registration status, available seller slots, listings, etc.
        
        Returns:
            Agent status as JSON
        """
        try:
            status = await agoragentic_get_agent_status()
            return json.dumps(status)
        except Exception as e:
            logger.error(f"Failed to get agent status: {e}")
            return json.dumps({"error": str(e)})
