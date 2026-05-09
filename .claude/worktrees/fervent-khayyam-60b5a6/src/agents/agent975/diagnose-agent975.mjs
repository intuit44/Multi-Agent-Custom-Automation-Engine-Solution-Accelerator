// diagnose-agent975.mjs - Script de diagnóstico para Agent975
import { AIProjectClient } from "@azure/ai-projects";
import { DefaultAzureCredential } from "@azure/identity";
import { config } from "dotenv";
import fs from "fs/promises";
import path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Cargar variables de entorno
config({ path: path.resolve(__dirname, "../../../.env") });

async function diagnose() {
  console.log("🔍 DIAGNÓSTICO DE AGENT975");
  console.log("═".repeat(50));

  // 1. Verificar archivos de configuración
  console.log("\n1️⃣ VERIFICANDO ARCHIVOS DE CONFIGURACIÓN:");

  const contextPath = path.resolve(__dirname, "../../../.codegpt/agents.context.json");
  const envPath = path.resolve(__dirname, "../../../.env");

  try {
    await fs.access(contextPath);
    console.log("✅ agents.context.json encontrado");

    const context = JSON.parse(await fs.readFile(contextPath, 'utf-8'));
    console.log("   Contenido:", JSON.stringify(context, null, 2));
  } catch (error) {
    console.log("❌ agents.context.json NO encontrado o inválido");
    console.log("   Error:", error.message);
  }

  try {
    await fs.access(envPath);
    console.log("✅ .env encontrado");

    // Mostrar variables relevantes (sin valores sensibles)
    const envVars = [
      'AZURE_AI_FOUNDRY_ENDPOINT',
      'AZURE_AI_FOUNDRY_PROJECT',
      'AGENT975_ID',
      'AZURE_TENANT_ID',
      'AZURE_CLIENT_ID'
    ];

    console.log("   Variables configuradas:");
    envVars.forEach(varName => {
      const value = process.env[varName];
      if (value) {
        console.log(`   ✓ ${varName}: ${value.substring(0, 10)}...`);
      } else {
        console.log(`   ✗ ${varName}: NO CONFIGURADA`);
      }
    });
  } catch (error) {
    console.log("❌ .env NO encontrado");
  }

  // 2. Verificar credenciales
  console.log("\n2️⃣ VERIFICANDO CREDENCIALES DE AZURE:");

  try {
    const credential = new DefaultAzureCredential();
    const token = await credential.getToken("https://cognitiveservices.azure.com/.default");
    console.log("✅ Token obtenido correctamente");
    console.log(`   Expira en: ${new Date(token.expiresOnTimestamp).toLocaleString()}`);
  } catch (error) {
    console.log("❌ Error al obtener token de Azure");
    console.log("   Error:", error.message);
    console.log("   Sugerencia: Ejecuta 'az login' o configura las credenciales de servicio");
  }

  // 3. Intentar conectar con el cliente
  console.log("\n3️⃣ PROBANDO CONEXIÓN CON AZURE AI FOUNDRY:");

  try {
    const context = JSON.parse(await fs.readFile(contextPath, 'utf-8'));
    const endpoint = process.env.AZURE_AI_FOUNDRY_ENDPOINT || context.endpoint;
    const project = process.env.AZURE_AI_FOUNDRY_PROJECT || context.project;
    const agentId = process.env.AGENT975_ID || context.agentId;

    console.log(`   Endpoint: ${endpoint}`);
    console.log(`   Proyecto: ${project}`);
    console.log(`   Agent ID: ${agentId}`);

    const credential = new DefaultAzureCredential();
    const client = new AIProjectClient(endpoint, credential);

    console.log("✅ Cliente creado correctamente");

    // 4. Verificar el agente
    console.log("\n4️⃣ VERIFICANDO AGENTE:");

    try {
      // Intentar diferentes métodos para verificar el agente
      console.log("   Intentando obtener información del agente...");

      // Método 1: getAgent
      try {
        const agent = await client.agents.getAgent(agentId);
        console.log("✅ Agente encontrado con getAgent()");
        console.log(`   ID: ${agent.id}`);
        console.log(`   Nombre: ${agent.name || 'Sin nombre'}`);
        console.log(`   Modelo: ${agent.model || 'No especificado'}`);
      } catch (e1) {
        console.log("⚠️  getAgent() falló:", e1.message);

        // Método 2: Listar agentes
        try {
          console.log("   Intentando listar agentes...");
          const agents = await client.agents.listAgents();
          console.log("✅ Lista de agentes obtenida");

          let found = false;
          for await (const agent of agents) {
            if (agent.id === agentId) {
              console.log(`✅ Agente ${agentId} encontrado en la lista`);
              found = true;
              break;
            }
          }

          if (!found) {
            console.log(`❌ Agente ${agentId} NO encontrado en la lista`);
          }
        } catch (e2) {
          console.log("❌ Error al listar agentes:", e2.message);
        }
      }

      // 5. Probar diferentes endpoints
      console.log("\n5️⃣ PROBANDO DIFERENTES FORMATOS DE ENDPOINT:");

      const endpointVariations = [
        endpoint,
        `${endpoint}/api/projects/${project}`,
        endpoint.replace('.services.ai.azure.com', '.api.azureml.ms'),
        endpoint.replace('https://', 'azureml://'),
      ];

      for (const ep of endpointVariations) {
        console.log(`\n   Probando: ${ep}`);
        try {
          const testClient = new AIProjectClient(ep, credential);
          // Intenta una operación simple
          await testClient.agents.listAgents();
          console.log("   ✅ Este formato funciona!");
          break;
        } catch (error) {
          console.log(`   ❌ Error: ${error.message.substring(0, 50)}...`);
        }
      }

    } catch (error) {
      console.log("❌ Error general al verificar agente:", error.message);
    }

  } catch (error) {
    console.log("❌ Error al conectar con Azure AI Foundry");
    console.log("   Error:", error.message);
  }

  // 6. Recomendaciones
  console.log("\n6️⃣ RECOMENDACIONES:");
  console.log("─".repeat(50));

  console.log(`
1. Asegúrate de que el agente '${process.env.AGENT975_ID || 'Agent975'}' existe en Azure AI Foundry
2. Verifica que las credenciales tienen permisos para acceder al proyecto
3. Confirma que el endpoint es correcto (puede que necesites usar .api.azureml.ms en lugar de .services.ai.azure.com)
4. Si usas Azure CLI, ejecuta: az login
5. Si usas Service Principal, verifica las variables AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, y AZURE_TENANT_ID
  `);

  console.log("\n🏁 Diagnóstico completado");
}

// Ejecutar diagnóstico
diagnose().catch(error => {
  console.error("\n💥 Error crítico durante el diagnóstico:", error);
  process.exit(1);
});