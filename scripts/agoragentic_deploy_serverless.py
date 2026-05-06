#!/usr/bin/env python3
"""Deploy a serverless function for LLM Router on Agoragentic relay."""

import asyncio
import json
from pathlib import Path

import httpx


# Simple serverless function code that will run on Agoragentic's relay platform
SERVERLESS_CODE = """
function handler(input) {
  const { task, type } = input;
  
  if (!task) {
    return { error: 'task parameter is required' };
  }
  
  return {
    task: task,
    type: type || 'general',
    status: 'received',
    message: 'LLM Router endpoint deployed successfully'
  };
}
"""


async def deploy_relay_function():
    """Deploy serverless function to Agoragentic relay platform."""
    
    # Load credentials
    creds_file = Path.home() / ".llm-router" / "agoragentic.json"
    if not creds_file.exists():
        print("❌ Agoragentic not configured. Run: python3 scripts/agoragentic_register.py")
        return
    
    creds = json.loads(creds_file.read_text())
    api_key = creds.get("api_key")
    
    # Deploy function to relay
    payload = {
        "name": "llm-router-relay",
        "source_code": SERVERLESS_CODE,
    }
    
    async with httpx.AsyncClient() as client:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        
        response = await client.post(
            "https://agoragentic.com/api/relay/deploy",
            json=payload,
            headers=headers,
            timeout=30.0,
        )
        
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            print(f"❌ Deploy Error: {e}")
            print(f"Response: {response.text}")
            return
        
        result = response.json()
        print("✅ Serverless function deployed!")
        print(json.dumps(result, indent=2))
        
        # Extract the relay URL
        relay_url = result.get("url")
        if relay_url:
            print(f"\n✅ Relay URL: {relay_url}")
            
            # Save relay URL
            relay_file = Path.home() / ".llm-router" / "agoragentic_relay.json"
            relay_file.write_text(json.dumps(result, indent=2))
            relay_file.chmod(0o600)
            print(f"✅ Relay info saved to {relay_file}")
        
        return result


if __name__ == "__main__":
    asyncio.run(deploy_relay_function())
