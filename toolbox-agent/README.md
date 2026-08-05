# What this sample demonstrates

An [Agent Framework](https://github.com/microsoft/agent-framework) agent that uses **Foundry Toolbox** for tool discovery and hosted using the **Responses protocol**. Foundry Toolbox is a managed tool registry in Microsoft Foundry that lets you define tools centrally and share them across agents.

## Creating a Foundry Toolbox
You can create a Foundry Toolbox in VSCode. Click [here](vscode://ms-windows-ai-studio.windows-ai-studio/open_tools) to create one in VSCode

You can also create a Foundry Toolbox by code. Refer to this sample for an example: [Foundry Toolbox CRUD Sample](https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/ai/azure-ai-projects/samples/hosted_agents/sample_toolboxes_crud.py).

You can also create a Foundry Toolbox in the Foundry portal. Read more about it [in the Foundry toolbox documentation](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/toolbox).


## How It Works

### Model Integration

The agent uses `FoundryChatClient` from the Agent Framework to create an OpenAI-compatible Responses client. It connects to the toolbox's MCP endpoint via `MCPStreamableHTTPTool`, which discovers and invokes the toolbox's tools over MCP at runtime.

See [main.py](main.py) for the full implementation.

### Agent Hosting

The agent is hosted using the [Agent Framework](https://github.com/microsoft/agent-framework) with the `ResponsesHostServer`, which provisions a REST API endpoint compatible with the OpenAI Responses protocol.

## Running the Agent Host

**Linux/macOS:**
```bash
# 1. Fill in the environment file
# Edit .env — set FOUNDRY_PROJECT_ENDPOINT, MODEL_DEPLOYMENT_NAME,
#              and TOOLBOX_ENDPOINT at minimum

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start the agent
python main.py

# 4. Invoke

# Option A — Agent Inspector in VS Code (recommended):
# Press F5 and select "Debug Local Agent HTTP Server".
# This starts the agent with debugging and opens the Agent Inspector —
# an interactive UI for sending messages, viewing tool calls, and debugging.

# Option B — curl:
curl -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input": "What tools do you have?"}'
```

**Windows (PowerShell):**
```powershell
# 1. Fill in the environment file
# Edit .env — set FOUNDRY_PROJECT_ENDPOINT, MODEL_DEPLOYMENT_NAME,
#              and TOOLBOX_ENDPOINT at minimum

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start the agent
python main.py

# 4. Invoke

# Option A — Agent Inspector in VS Code (recommended):
# Press F5 and select "Debug Local Agent HTTP Server".
# This starts the agent with debugging and opens the Agent Inspector.

# Option B — Invoke-RestMethod:
Invoke-RestMethod -Method POST http://localhost:8088/responses `
  -ContentType "application/json" `
  -Body '{"input": "What tools do you have?"}'
```

### Test in Agent Inspector

Once the agent is running locally, open **Agent Inspector** in VS Code (Command Palette: **Foundry Toolkit: Open Agent Inspector**) to interactively send messages and view responses.

Type the following message in Inspector:

```
What tools do you have?
```

### Deploying with the Foundry Toolkit VS Code Extension

1. Open the Command Palette (`Ctrl+Shift+P`) and run **Foundry Toolkit: Deploy Hosted Agent**. The extension opens a tab-based **Deploy Hosted Agent** wizard and reads `agent.yaml` to auto-populate what it can.
2. If prompted, complete **Foundry Project Setup** to pick the subscription and Foundry project (or create a new one) to deploy to.
3. On the **Basics** tab, configure the core deployment settings:
   - **Deployment Method**: **Code** (upload as a ZIP) or **Container** (Docker image via ACR).
   - For **Code**, pick a packaging option: **Remote** or **Local**.
   - For **Container**, pick a registry option: default ACR, your own ACR, or a prebuilt ACR image.
   - **Hosted Agent Name**: confirm the name to register with the hosting service.
4. On the **Review + Deploy** tab, finalize the runtime and resources:
   - Confirm the auto-detected runtime details (language, entry point, or Dockerfile).
   - Pick a **CPU and Memory** size.
   - Click **Deploy**. Fields are validated inline, and the extension handles the build/upload, agent version creation, and RBAC role assignment.
5. After deployment, invoke the agent in the Agent Playground and stream live logs from the **Logs** tab.
