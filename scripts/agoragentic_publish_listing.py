#!/usr/bin/env python3
"""Publish llm-router's first listing on Agoragentic marketplace."""

import asyncio
import json
from pathlib import Path

import httpx


async def publish_listing():
    """Publish llm-router LLM routing service on Agoragentic."""
    
    # Load credentials
    creds_file = Path.home() / ".llm-router" / "agoragentic.json"
    if not creds_file.exists():
        print("❌ Agoragentic not configured. Run: python3 scripts/agoragentic_register.py")
        return
    
    creds = json.loads(creds_file.read_text())
    api_key = creds.get("api_key")
    
    # Define the listing
    listing = {
        "name": "LLM Routing Service",
        "description": (
            "Intelligent routing for language model tasks. Selects optimal models from multiple providers. "
            "Optimizes for quality and cost efficiency."
        ),
        "category": "ai-optimization",
        "listing_type": "service",
        "pricing_model": "per_call",
        "price_per_unit": 0.0,  # Free listing
        "endpoint_url": "relay://function/f2456d76-eaa2-44f9-9c6c-b221a6c4be6e",  # Deployed relay function
        "input_schema": {
            "type": "object",
            "required": ["task"],
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Task to execute",
                },
                "type": {
                    "type": "string",
                    "description": "Task type",
                },
            },
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": "Model used",
                },
                "output": {
                    "type": "string",
                    "description": "Result",
                },
            },
        },
        "tags": ["llm", "routing", "multi-provider", "ai"],
        "sandbox_probe_input": {
            "task": "Test routing request",
            "type": "general",
        },
    }
    
    async with httpx.AsyncClient() as client:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        
        response = await client.post(
            "https://agoragentic.com/api/capabilities",
            json=listing,
            headers=headers,
            timeout=30.0,
        )
        
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            print(f"❌ API Error: {e}")
            print(f"Response: {response.text}")
            return
        
        result = response.json()
        
        print("✅ Listing published successfully!")
        print(json.dumps(result, indent=2))
        
        # Save listing info
        listing_file = Path.home() / ".llm-router" / "agoragentic_listing.json"
        listing_file.write_text(json.dumps(result, indent=2))
        listing_file.chmod(0o600)
        
        print(f"\n✅ Listing info saved to {listing_file}")
        
        return result


if __name__ == "__main__":
    asyncio.run(publish_listing())
