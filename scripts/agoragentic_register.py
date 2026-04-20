#!/usr/bin/env python3
"""Register llm-router with Agoragentic platform."""

import asyncio
import json
import os
from pathlib import Path

import httpx


async def register_on_agoragentic():
    """Register llm-router as an agent on Agoragentic."""
    
    async with httpx.AsyncClient() as client:
        # Register via quickstart endpoint
        response = await client.post(
            "https://agoragentic.com/api/quickstart",
            json={
                "name": "llm-router-saving-tokens",
                "description": "Smart LLM routing with cost optimization, budget control, and 20+ AI providers. Reduces token costs by up to 90% through intelligent model selection based on task complexity, budget pressure, and quality metrics.",
            },
            timeout=30.0,
        )
        
        response.raise_for_status()
        result = response.json()
        
        print("✅ Registration successful!")
        print(json.dumps(result, indent=2))
        
        # Save credentials
        config_dir = Path.home() / ".llm-router"
        config_dir.mkdir(exist_ok=True)
        
        creds_file = config_dir / "agoragentic.json"
        creds_file.write_text(json.dumps(result, indent=2))
        creds_file.chmod(0o600)  # Secure permissions
        
        print(f"\n✅ Credentials saved to {creds_file}")
        print(f"API Key: {result.get('api_key', 'N/A')}")
        print(f"Agent ID: {result.get('agent_id', 'N/A')}")
        
        return result


if __name__ == "__main__":
    asyncio.run(register_on_agoragentic())
