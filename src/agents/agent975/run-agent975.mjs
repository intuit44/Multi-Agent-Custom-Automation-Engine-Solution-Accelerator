// run-agent975.mjs - Script principal del Agent975
import { AIProjectClient } from "@azure/ai-projects";
import { DefaultAzureCredential } from "@azure/identity";
import { config } from "dotenv";
import path from "path";
import { fileURLToPath } from "url";

// Configurar rutas
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Cargar variables de entorno desde la raíz
config({ path: path.resolve(__dirname, "../../../.env") });

class Agent975 {
  constructor() {
    this.baseUrl = "https://eastus.api.azureml.ms/mlflow/v1.0/subscriptions/380fa841-83f3-42fe-adc4-582a5ebe139b/resourceGroups/rg-DEV/providers/Microsoft.MachineLearningServices/workspaces/ml-foundry-prod";
    this.credential = new DefaultAzureCredential();
    this.currentRun = null;
  }

  async initialize() {
    try {
      // Configuración de endpoints y variables
      this.config = {
        mlflowEndpoint: process.env.AZURE_ML_TRACKING_URI || "azureml://eastus.api.azureml.ms/mlflow/v1.0/subscriptions/380fa841-83f3-42fe-adc4-582a5ebe139b/resourceGroups/rg-DEV/providers/Microsoft.MachineLearningServices/workspaces/ml-foundry-prod",
        apiEndpoint: process.env.AZURE_ML_ENDPOINT || "https://eastus.api.azureml.ms",
        project: process.env.AZURE_AI_FOUNDRY_PROJECT,
        agentId: process.env.AGENT975_ID
      };

      // Prueba de saneamiento: Asegurarse de que los endpoints sean cadenas y estén limpios
      if (this.config.mlflowEndpoint) {
        this.config.mlflowEndpoint = this.config.mlflowEndpoint.toString().trim();
      }
      if (this.config.apiEndpoint) {
        this.config.apiEndpoint = this.config.apiEndpoint.toString().trim();
      }

      // Validar configuración
      this.validateConfig();

      // Inicializar cliente
      const credential = new DefaultAzureCredential();
      this.client = new AIProjectClient(this.config.apiEndpoint, credential);

      console.log("✅ Agent975 inicializado correctamente");
      console.log(`📍 MLflow Endpoint: ${this.config.mlflowEndpoint}`);
      console.log(`📍 API Endpoint: ${this.config.apiEndpoint}`);
      console.log(`📁 Proyecto: ${this.config.project}`);
      console.log(`🤖 Agent ID: ${this.config.agentId}`);

    } catch (error) {
      console.error("❌ Error al inicializar Agent975:", error.message);
      throw error;
    }
  }

  validateConfig() {
    const required = ["mlflowEndpoint", "apiEndpoint", "project", "agentId"];
    const missing = required.filter(field => !this.config[field]);

    if (missing.length > 0) {
      throw new Error(`Faltan campos requeridos: ${missing.join(", ")}`);
    }

    // Validar formato de endpoints
    const mlflowPattern = /^azureml:\/\/.*\.api\.azureml\.ms\/mlflow\/v1\.0\/subscriptions\/[a-f0-9-]+\/resourceGroups\/.+\/providers\/Microsoft.MachineLearningServices\/workspaces\/.+$/;
    const apiPattern = /^https:\/\/[a-zA-Z0-9-]+\.api\.azureml\.ms$/;

    if (!mlflowPattern.test(this.config.mlflowEndpoint)) {
      throw new Error(`Formato incorrecto para MLflow endpoint: ${this.config.mlflowEndpoint}`);
    }

    if (!apiPattern.test(this.config.apiEndpoint)) {
      throw new Error(`Formato incorrecto para API endpoint: ${this.config.apiEndpoint}`);
    }

    console.log("[DEBUG] Validación de configuración completada.");
  }

  async analyzeCode(code, options = {}) {
    try {
      console.log("\n🔍 Analizando código TSX...");

      // Crear un hilo (thread)
      const thread = await this.client.agents.threads.create();

      // Enviar el código como un mensaje
      await this.client.agents.messages.create(thread.id, "user", code);

      // Ejecutar el agente
      let run = await this.client.agents.runs.create(thread.id, this.config.agentId);

      // Esperar hasta que finalice
      while (run.status === "queued" || run.status === "in_progress") {
        await new Promise(resolve => setTimeout(resolve, 1000));
        run = await this.client.agents.runs.get(thread.id, run.id);
      }

      if (!run || run.status === "failed") {
        const msg = run?.lastError?.message || "Run no se completó o falló sin mensaje.";
        throw new Error(`Run failed: ${msg}`);
      }
      console.log("[DEBUG] run object:", JSON.stringify(run, null, 2));

      // Recuperar los mensajes del hilo
      const messages = await this.client.agents.messages.list(thread.id, { order: "asc" });

      const result = [];
      for (const message of messages) {
        const content = message.content.find(c => c.type === "text" && "text" in c);
        if (content) result.push(`${message.role}: ${content.text.value}`);
      }

      return result;

    } catch (error) {
      console.error("❌ Error al analizar código:", error?.message || JSON.stringify(error));
      throw error;
    }
  }

  async startExperiment(experimentName) {
    // Crear o obtener experimento
    const expUrl = `${this.baseUrl}/api/2.0/mlflow/experiments/create`;
    const expResponse = await this._makeRequest(expUrl, 'POST', { name: experimentName });

    // Iniciar run
    const runUrl = `${this.baseUrl}/api/2.0/mlflow/runs/create`;
    const runResponse = await this._makeRequest(runUrl, 'POST', {
      experiment_id: expResponse.experiment_id,
      start_time: Math.floor(Date.now() / 1000)
    });

    this.currentRun = runResponse.run.info;
    return this.currentRun;
  }

  async logMetrics(metrics) {
    if (!this.currentRun) throw new Error("No hay un run activo");

    const url = `${this.baseUrl}/api/2.0/mlflow/runs/log-metric`;
    const promises = Object.entries(metrics).map(([key, value]) =>
      this._makeRequest(url, 'POST', {
        run_id: this.currentRun.run_id,
        key,
        value,
        timestamp: Math.floor(Date.now() / 1000),
        step: 0
      })
    );

    return Promise.all(promises);
  }

  async endRun(status = "FINISHED") {
    if (!this.currentRun) throw new Error("No hay un run activo");

    const url = `${this.baseUrl}/api/2.0/mlflow/runs/update`;
    const response = await this._makeRequest(url, 'POST', {
      run_id: this.currentRun.run_id,
      status,
      end_time: Math.floor(Date.now() / 1000)
    });

    this.currentRun = null;
    return response;
  }

  async _makeRequest(url, method, body) {
    const token = await this.credential.getToken("https://ml.azure.com/.default");

    const response = await fetch(url, {
      method,
      headers: {
        "Authorization": `Bearer ${token.token}`,
        "Content-Type": "application/json"
      },
      body: JSON.stringify(body)
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(`Error en ${method} ${url}: ${response.status} - ${JSON.stringify(errorData)}`);
    }

    return response.json();
  }
}

export default Agent975;

// Uso programático del Agent975
async function programmaticUsage() {
  try {
    const agent = new Agent975();
    await agent.initialize();

    const code = `
    const MyComponent = ({ data }) => {
      return <FlatList data={data} renderItem={renderItem} />;
    };
    `;

    const result = await agent.analyzeCode(code, {
      complexity: true,
      performance: true,
      suggestions: true
    });

    console.log("\n✅ Resultado del análisis:");
    console.log(result);
  } catch (error) {
    console.error("\n❌ Error durante el uso programático:", error.message);
  }
}

// Ejecutar el uso programático
programmaticUsage();

// Uso ejemplo
const agent = new Agent975();

async function runExample() {
  try {
    // 1. Iniciar experimento
    const runInfo = await agent.startExperiment("mi-experimento");
    console.log("Run iniciado:", runInfo.run_id);

    // 2. Registrar métricas
    await agent.logMetrics({
      accuracy: 0.92,
      precision: 0.95,
      recall: 0.89
    });
    console.log("Métricas registradas");

    // 3. Finalizar run
    await agent.endRun();
    console.log("Run completado exitosamente");
  } catch (error) {
    console.error("Error en el flujo:", error);
  }
}

runExample();