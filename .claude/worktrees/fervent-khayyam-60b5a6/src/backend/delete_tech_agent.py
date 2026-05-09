#!/usr/bin/env python
"""Delete TechSupportAgent to force recreation with new functions."""

import asyncio
from app_config import config


async def delete_agent():
    client = config.get_ai_project_client()
    agent_id = "asst_ycHCfJ8fZCF7OcsWAo5iC4DL"

    try:
        await client.agents.delete_agent(agent_id)
        print(f"✅ Deleted agent {agent_id}")
    except Exception as e:
        print(f"❌ Failed to delete agent: {e}")


if __name__ == "__main__":
    asyncio.run(delete_agent())
