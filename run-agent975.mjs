import { AIProjectClient } from "@azure/ai-projects";
import { DefaultAzureCredential } from "@azure/identity";

async function runAgentConversation() {
  const project = new AIProjectClient(
    "https://boatRentalFoundry-dev.services.ai.azure.com/api/projects/booking-agents",
    new DefaultAzureCredential()
  );

  const agent = await project.agents.getAgent("asst_jjH5up8ROP1hF0sRYoNZyFNQ");
  console.log(`Retrieved agent: ${agent.name}`);

  const thread = await project.agents.threads.create();
  console.log(`Created thread, ID: ${thread.id}`);

  const message = await project.agents.messages.create(thread.id, "user", "Hello Agent");
  console.log(`Created message, ID: ${message.id}`);

  let run = await project.agents.runs.create(thread.id, agent.id);

  while (run.status === "queued" || run.status === "in_progress") {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    run = await project.agents.runs.get(thread.id, run.id);
  }

  if (run.status === "failed") {
    console.error(`Run failed: `, run.lastError);
  }

  console.log(`Run completed with status: ${run.status}`);

  const messages = await project.agents.messages.list(thread.id, { order: "asc" });

  for await (const m of messages) {
    const content = m.content.find((c) => c.type === "text" && "text" in c);
    if (content) {
      console.log(`${m.role}: ${content.text.value}`);
    }
  }
}

runAgentConversation().catch((error) => {
  console.error("An error occurred:", error);
});
