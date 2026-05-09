// run-agent975-fixed.mjs - Versión corregida para Windows
import { DefaultAzureCredential } from "@azure/identity";
import { config } from "dotenv";
import fs from "fs/promises";
import path from "path";
import { fileURLToPath } from "url";

// Verificar dependencias al inicio
console.log("🚀 Iniciando Agent975...");

let fetch;
try {
  const fetchModule = await import("node-fetch");
  fetch = fetchModule.default;
  console.log("✅ node-fetch cargado correctamente");
} catch (error) {
  console.error("❌ Error: Falta instalar node-fetch");
  console.error("   Ejecuta: npm install node-fetch");
  process.exit(1);
}

// Configurar rutas
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Cargar variables de entorno
const envResult = config({ path: path.resolve(__dirname, "../../../.env") });
console.log(`📁 Archivo .env: ${envResult.parsed ? 'Cargado' : 'No encontrado'}`);

class Agent975Runner {
  constructor() {
    this.contextPath = path.resolve(__dirname, "../../../.codegpt/agents.context.json");
    this.config = null;
    this.credential = null;
  }

  async initialize() {
    console.log("\n🔧 Inicializando Agent975...");

    try {
      // Verificar archivo de contexto
      console.log("📄 Buscando archivo de contexto...");
      try {
        await fs.access(this.contextPath);
        console.log(`✅ Archivo encontrado: ${this.contextPath}`);
      } catch {
        console.error(`❌ No se encontró: ${this.contextPath}`);
        // Intentar ruta alternativa
        this.contextPath = path.resolve(__dirname, "../../../agents.context.json");
        console.log(`🔄 Intentando ruta alternativa: ${this.contextPath}`);
        await fs.access(this.contextPath);
      }

      // Cargar configuración
      const contextContent = await fs.readFile(this.contextPath, "utf-8");
      this.config = JSON.parse(contextContent);
      console.log("✅ Configuración cargada:");
      console.log(JSON.stringify(this.config, null, 2));

      // Verificar credenciales
      console.log("\n🔑 Verificando credenciales de Azure...");
      this.credential = new DefaultAzureCredential();

      const token = await this.credential.getToken("https://ml.azure.com/.default");
      console.log("✅ Token obtenido correctamente");
      console.log(`   Expira: ${new Date(token.expiresOnTimestamp).toLocaleString()}`);

      console.log("\n✅ Agent975 inicializado correctamente");
      console.log(`📍 Endpoint: ${this.config.endpoint}`);
      console.log(`📁 Workspace: ${this.config.workspace}`);
      console.log(`🤖 Agent ID: ${this.config.agentId}`);

    } catch (error) {
      console.error("\n❌ Error durante la inicialización:");
      console.error(error.message);

      if (error.message.includes("ENOENT")) {
        console.log("\n💡 Asegúrate de que existe el archivo agents.context.json");
        console.log("   en una de estas ubicaciones:");
        console.log(`   - ${path.resolve(__dirname, "../../../.codegpt/agents.context.json")}`);
        console.log(`   - ${path.resolve(__dirname, "../../../agents.context.json")}`);
      }

      throw error;
    }
  }

  validateExperimentName(name) {
    // MLflow permite máximo 250 caracteres y ciertos caracteres especiales
    if (name.length > 250) return false;
    if (!/^[a-zA-Z0-9_\-\.]+$/.test(name)) return false;
    return true;
  }

  generateExperimentName() {
    const baseName = "agent975";
    const timestamp = new Date().toISOString().replace(/[:\-T\.Z]/g, '');
    const name = `${baseName}-${timestamp}`;
    return this.validateExperimentName(name) ? name : `${baseName}-${Date.now()}`;
  }

  async createExperiment(experimentName = null) {
    if (!experimentName) {
      experimentName = this.generateExperimentName();
    }

    console.log(`\n🧪 Creando/obteniendo experimento: ${experimentName}`);

    try {
      const token = await this.credential.getToken("https://ml.azure.com/.default");

      // Construir la URL correcta de MLflow con la información del workspace
      const mlflowBaseUrl = `${this.config.endpoint}/mlflow/v1.0/subscriptions/${this.config.subscriptionId}/resourceGroups/${this.config.resourceGroup}/providers/Microsoft.MachineLearningServices/workspaces/${this.config.workspace}`;

      // Buscar experimento existente
      const searchUrl = `${mlflowBaseUrl}/api/2.0/mlflow/experiments/search`;
      console.log(`📍 Buscando en: ${searchUrl}`);

      const searchResponse = await fetch(searchUrl, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${token.token}`,
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          filter: `name = '${experimentName}'`
        })
      });

      console.log(`📊 Búsqueda respuesta: ${searchResponse.status}`);

      if (searchResponse.ok) {
        const data = await searchResponse.json();
        if (data.experiments && data.experiments.length > 0) {
          console.log(`✅ Experimento existente encontrado: ${data.experiments[0].experiment_id}`);
          return data.experiments[0].experiment_id;
        }
      }

      // Crear nuevo experimento
      console.log("📝 Creando nuevo experimento...");
      const createUrl = `${mlflowBaseUrl}/api/2.0/mlflow/experiments/create`;

      const createResponse = await fetch(createUrl, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${token.token}`,
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          name: experimentName,
          artifact_location: `mlflow-artifacts:/${experimentName}`,
          tags: [
            { key: "mlflow.user", value: "Agent975" },
            { key: "created.from", value: "nodejs" },
            { key: "purpose", value: "tsx-analysis" }
          ]
        })
      });

      if (createResponse.ok) {
        const data = await createResponse.json();
        console.log(`✅ Nuevo experimento creado: ${data.experiment_id}`);
        return data.experiment_id;
      } else {
        const errorText = await createResponse.text();
        console.error("❌ Error al crear experimento:", errorText);
        throw new Error("No se pudo crear el experimento");
      }

    } catch (error) {
      console.error("❌ Error con experimento:", error.message);
      // Continuar sin experimento
      return "default";
    }
  }

  analyzeCodeLocally(code) {
    console.log("\n🔍 Analizando código TSX...");

    const lines = code.split('\n').filter(line => line.trim()).length;
    const size = code.length;

    // Análisis básico
    const hasState = code.includes('useState') || code.includes('useReducer');
    const hasEffects = code.includes('useEffect');
    const hasFlatList = code.includes('FlatList');
    const hasKeyExtractor = code.includes('keyExtractor');

    const metrics = {
      lines,
      size,
      complexity: hasState ? 5 : 3,
      hasOptimizations: hasKeyExtractor
    };

    const suggestions = [];

    if (hasFlatList && !hasKeyExtractor) {
      suggestions.push("⚠️ Añade keyExtractor a FlatList para mejor rendimiento");
    }

    if (!code.includes('React.memo')) {
      suggestions.push("💡 Considera usar React.memo para optimizar re-renders");
    }

    const analysis = {
      timestamp: new Date().toISOString(),
      metrics,
      feedback: [
        `✅ Código TSX analizado correctamente`,
        `📏 Líneas de código: ${lines}`,
        `📦 Tamaño: ${size} caracteres`,
        hasState ? "✓ Usa estado" : "✓ Componente sin estado",
        hasFlatList ? "✓ Usa FlatList" : ""
      ].filter(Boolean),
      suggestions
    };

    return analysis;
  }

  async runAnalysis(code) {
    try {
      // Intentar crear experimento
      let experimentId = "local";
      try {
        experimentId = await this.createExperiment();

        // Si tenemos un experimento válido, crear un run
        if (experimentId && experimentId !== "local") {
          await this.createMLflowRun(experimentId, code);
        }
      } catch (e) {
        console.log("⚠️  Continuando sin MLflow tracking:", e.message);
      }

      // Realizar análisis
      const analysis = this.analyzeCodeLocally(code);

      // Mostrar resultados
      console.log("\n📊 RESULTADOS DEL ANÁLISIS:");
      console.log("─".repeat(50));

      console.log("\n📋 Feedback:");
      analysis.feedback.forEach(item => console.log(`   ${item}`));

      console.log("\n📈 Métricas:");
      Object.entries(analysis.metrics).forEach(([key, value]) => {
        console.log(`   ${key}: ${value}`);
      });

      if (analysis.suggestions.length > 0) {
        console.log("\n💡 Sugerencias:");
        analysis.suggestions.forEach(s => console.log(`   ${s}`));
      }

      return analysis;

    } catch (error) {
      console.error("❌ Error en análisis:", error.message);
      throw error;
    }
  }

  async createMLflowRun(experimentId, code) {
    try {
      const token = await this.credential.getToken("https://ml.azure.com/.default");
      const mlflowBaseUrl = `${this.config.endpoint}/mlflow/v1.0/subscriptions/${this.config.subscriptionId}/resourceGroups/${this.config.resourceGroup}/providers/Microsoft.MachineLearningServices/workspaces/${this.config.workspace}`;

      // Crear run
      const runUrl = `${mlflowBaseUrl}/api/2.0/mlflow/runs/create`;

      const runResponse = await fetch(runUrl, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${token.token}`,
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          experiment_id: experimentId,
          tags: [
            { key: "agent", value: "Agent975" },
            { key: "analysis_type", value: "tsx_code" }
          ]
        })
      });

      if (runResponse.ok) {
        const runData = await runResponse.json();
        console.log(`🚀 Run MLflow creado: ${runData.run.info.run_id}`);

        // Registrar métricas básicas
        const metrics = this.analyzeCodeLocally(code).metrics;
        await this.logMLflowMetrics(runData.run.info.run_id, metrics, token.token);
      }
    } catch (error) {
      console.log("⚠️  No se pudo crear run MLflow:", error.message);
    }
  }

  async logMLflowMetrics(runId, metrics, token) {
    try {
      const mlflowBaseUrl = `${this.config.endpoint}/mlflow/v1.0/subscriptions/${this.config.subscriptionId}/resourceGroups/${this.config.resourceGroup}/providers/Microsoft.MachineLearningServices/workspaces/${this.config.workspace}`;
      const url = `${mlflowBaseUrl}/api/2.0/mlflow/runs/log-batch`;

      const metricsData = Object.entries(metrics).map(([key, value]) => ({
        key,
        value: value.toString(),
        timestamp: Date.now()
      }));

      await fetch(url, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${token}`,
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          run_id: runId,
          metrics: metricsData
        })
      });

      console.log("📊 Métricas registradas en MLflow");
    } catch (error) {
      console.log("⚠️  No se pudieron registrar métricas:", error.message);
    }
  }
}

// Función principal
async function runMain() {
  console.log("═".repeat(60));
  console.log("        AGENT975 - AZURE ML INTEGRATION");
  console.log("═".repeat(60));

  const agent = new Agent975Runner();

  try {
    // Inicializar
    await agent.initialize();

    // Código de ejemplo
    const sampleCode = `
import React from 'react';
import { FlatList, View, Text } from 'react-native';

const BoatList = ({ boats }) => {
  const renderItem = ({ item }) => (
    <View style={{ padding: 10 }}>
      <Text>{item.name}</Text>
      <Text>{\`\${item.price}/día\`}</Text>
    </View>
  );

  return (
    <FlatList
      data={boats}
      renderItem={renderItem}
      keyExtractor={item => item.id}
    />
  );
};

export default BoatList;
`;

    // Ejecutar análisis
    const analysis = await agent.runAnalysis(sampleCode);

    // Guardar resultado
    const outputPath = path.join(__dirname, "analysis-result.json");
    await fs.writeFile(outputPath, JSON.stringify(analysis, null, 2));
    console.log(`\n💾 Resultado guardado en: ${outputPath}`);

    console.log("\n✅ Proceso completado exitosamente");

  } catch (error) {
    console.error("\n💥 ERROR FATAL:");
    console.error("Mensaje:", error.message);
    console.error("\nStack trace:");
    console.error(error.stack);
    process.exit(1);
  }
}

// Ejecutar directamente
console.log("🏁 Ejecutando Agent975...\n");
runMain().catch(error => {
  console.error("\n💥 Error no capturado:");
  console.error(error);
  process.exit(1);
});