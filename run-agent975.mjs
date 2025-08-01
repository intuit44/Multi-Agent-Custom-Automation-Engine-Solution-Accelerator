import { AIProjectClient } from "@azure/ai-projects";
import { DefaultAzureCredential } from "@azure/identity";
import fs from "fs";
import path from "path";

async function runAgentConversation() {
  const __dirname = path.dirname(new URL(import.meta.url).pathname);
  const contextPath = path.resolve(__dirname, "../../../.codegpt/agents.context.json");
  const { endpoint, project, agentId } = JSON.parse(fs.readFileSync(contextPath, "utf-8"));

  const projectClient = new AIProjectClient(
    `${endpoint}/api/projects/${project}`,
    new DefaultAzureCredential());

  const agent = await projectClient.agents.getAgent(agentId);
  console.log(`Retrieved agent: ${agent.name}`);

  const thread = await projectClient.agents.threads.create();
  console.log(`Created thread, ID: ${thread.id}`);

  const message = await projectClient.agents.messages.create(thread.id, "user", "Hello Agent");
  console.log(`Created message, ID: ${message.id}`);

  // Create run
  let run = await projectClient.agents.runs.create(thread.id, agent.id);

  // Poll until the run reaches a terminal status
  while (run.status === "queued" || run.status === "in_progress") {
    // Wait for a second
    await new Promise((resolve) => setTimeout(resolve, 1000));
    run = await projectClient.agents.runs.get(thread.id, run.id);
  }

  if (run.status === "failed") {
    console.error(`Run failed: `, run.lastError);
  }

  console.log(`Run completed with status: ${run.status}`);

  // Retrieve messages
  const messages = await project.agents.messages.list(thread.id, { order: "asc" });

  // Display messages
  for await (const m of messages) {
    const content = m.content.find((c) => c.type === "text" && "text" in c);
    if (content) {
      console.log(`${m.role}: ${content.text.value}`);
    }
  }
}

// Main execution
runAgentConversation().catch(error => {
  console.error("An error occurred:", error);
});
