// run-agent975-azureml.mjs - Integración con Azure Machine Learning
import { DefaultAzureCredential } from "@azure/identity";
import { config } from "dotenv";
import fs from "fs/promises";
import fetch from "node-fetch";
import path from "path";
import { fileURLToPath } from "url";

// Configurar rutas
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Cargar variables de entorno
config({ path: path.resolve(__dirname, "../../../.env") });

class Agent975AzureML {
  constructor() {
    this.contextPath = path.resolve(__dirname, "../../../.codegpt/agents.context.json");
    this.config = null;
    this.credential = null;
    this.mlflowEndpoint = null;
  }

  async initialize() {
    try {
      // Cargar configuración
      const contextContent = await fs.readFile(this.contextPath, "utf-8");
      this.config = JSON.parse(contextContent);

      // Construir endpoint MLflow
      this.mlflowEndpoint = `azureml://eastus.api.azureml.ms/mlflow/v1.0/subscriptions/${this.config.subscriptionId}/resourceGroups/${this.config.resourceGroup}/providers/Microsoft.MachineLearningServices/workspaces/${this.config.workspace}`;

      // Inicializar credenciales
      this.credential = new DefaultAzureCredential();

      console.log("✅ Agent975 AzureML inicializado correctamente");
      console.log(`📍 MLflow Endpoint: ${this.mlflowEndpoint}`);
      console.log(`📍 API Endpoint: ${this.config.endpoint}`);
      console.log(`📁 Workspace: ${this.config.workspace}`);

    } catch (error) {
      console.error("❌ Error al inicializar:", error.message);
      throw error;
    }
  }

  async getAccessToken() {
    try {
      const tokenResponse = await this.credential.getToken("https://ml.azure.com/.default");
      return tokenResponse.token;
    } catch (error) {
      console.error("❌ Error al obtener token:", error.message);
      throw error;
    }
  }

  async createOrGetExperiment(experimentName = "agent975-experiments") {
    try {
      const token = await this.getAccessToken();
      const baseUrl = `${this.config.endpoint}/mlflow/v2.0`;

      // Intentar obtener el experimento
      const searchUrl = `${baseUrl}/api/2.0/mlflow/experiments/search`;
      const searchResponse = await fetch(searchUrl, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${token}`,
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          filter: `name = '${experimentName}'`
        })
      });

      if (searchResponse.ok) {
        const data = await searchResponse.json();
        if (data.experiments && data.experiments.length > 0) {
          console.log(`📊 Experimento existente encontrado: ${data.experiments[0].experiment_id}`);
          return data.experiments[0].experiment_id;
        }
      }

      // Crear nuevo experimento
      const createUrl = `${baseUrl}/api/2.0/mlflow/experiments/create`;
      const createResponse = await fetch(createUrl, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${token}`,
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          name: experimentName,
          tags: {
            "agent": "Agent975",
            "purpose": "tsx-analysis"
          }
        })
      });

      if (createResponse.ok) {
        const data = await createResponse.json();
        console.log(`✅ Nuevo experimento creado: ${data.experiment_id}`);
        return data.experiment_id;
      }

      throw new Error("No se pudo crear el experimento");

    } catch (error) {
      console.error("❌ Error con experimento:", error.message);
      throw error;
    }
  }

  async runAnalysis(code, experimentId) {
    try {
      const token = await this.getAccessToken();
      const baseUrl = `${this.config.endpoint}/mlflow/v2.0`;

      // Crear un nuevo run
      const runUrl = `${baseUrl}/api/2.0/mlflow/runs/create`;
      const runResponse = await fetch(runUrl, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${token}`,
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          experiment_id: experimentId,
          tags: {
            "agent": "Agent975",
            "analysis_type": "tsx_code"
          }
        })
      });

      if (!runResponse.ok) {
        throw new Error(`Error al crear run: ${runResponse.status}`);
      }

      const runData = await runResponse.json();
      const runId = runData.run.info.run_id;

      console.log(`🚀 Run iniciado: ${runId}`);

      // Analizar el código (simulación local por ahora)
      const analysis = this.analyzeCodeLocally(code);

      // Registrar métricas
      await this.logMetrics(runId, analysis.metrics, token);

      // Registrar parámetros
      await this.logParams(runId, {
        code_lines: analysis.lines,
        code_size: analysis.size,
        component_type: analysis.componentType
      }, token);

      // Finalizar run
      await this.updateRunStatus(runId, "FINISHED", token);

      console.log("✅ Análisis completado");
      return analysis;

    } catch (error) {
      console.error("❌ Error en análisis:", error.message);
      throw error;
    }
  }

  analyzeCodeLocally(code) {
    const lines = code.split('\n').filter(line => line.trim()).length;
    const size = code.length;

    // Análisis básico del código TSX
    const hasState = code.includes('useState') || code.includes('useReducer');
    const hasEffects = code.includes('useEffect');
    const hasProps = code.includes('Props') || code.includes('props');
    const hasFlatList = code.includes('FlatList');
    const hasKeyExtractor = code.includes('keyExtractor');

    const componentType = code.includes('React.FC') ? 'Functional Component' :
      code.includes('class') ? 'Class Component' : 'Simple Component';

    const metrics = {
      complexity: hasState ? 5 : 3 + (hasEffects ? 2 : 0),
      performance_score: hasFlatList && hasKeyExtractor ? 8 : 6,
      best_practices_score: hasProps ? 7 : 5
    };

    const suggestions = [];

    if (hasFlatList && !hasKeyExtractor) {
      suggestions.push("⚠️ FlatList debe incluir keyExtractor para mejor rendimiento");
    }

    if (!code.includes('React.memo') && componentType === 'Functional Component') {
      suggestions.push("💡 Considera usar React.memo para optimizar re-renders");
    }

    if (code.includes('onPress') && !code.includes('useCallback')) {
      suggestions.push("💡 Usa useCallback para funciones pasadas como props");
    }

    return {
      lines,
      size,
      componentType,
      metrics,
      feedback: [
        `✅ Código TSX recibido correctamente`,
        `📏 Líneas de código: ${lines}`,
        `📦 Tamaño: ${size} caracteres`,
        `🧩 Tipo: ${componentType}`,
        hasState ? "✓ Maneja estado" : "✓ Componente sin estado",
        hasFlatList ? "✓ Usa FlatList para listas" : ""
      ].filter(Boolean),
      suggestions
    };
  }

  async logMetrics(runId, metrics, token) {
    const baseUrl = `${this.config.endpoint}/mlflow/v2.0`;
    const url = `${baseUrl}/api/2.0/mlflow/runs/log-batch`;

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

    console.log("📊 Métricas registradas");
  }

  async logParams(runId, params, token) {
    const baseUrl = `${this.config.endpoint}/mlflow/v2.0`;
    const url = `${baseUrl}/api/2.0/mlflow/runs/log-batch`;

    const paramsData = Object.entries(params).map(([key, value]) => ({
      key,
      value: value.toString()
    }));

    await fetch(url, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${token}`,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        run_id: runId,
        params: paramsData
      })
    });
  }

  async updateRunStatus(runId, status, token) {
    const baseUrl = `${this.config.endpoint}/mlflow/v2.0`;
    const url = `${baseUrl}/api/2.0/mlflow/runs/update`;

    await fetch(url, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${token}`,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        run_id: runId,
        status: status
      })
    });

    console.log(`✅ Run completado exitosamente`);
  }
}

// Función principal
async function main() {
  const agent = new Agent975AzureML();

  try {
    await agent.initialize();

    // Código de ejemplo para analizar
    const sampleCode = `
import React from 'react';
import { FlatList, View, Text, TouchableOpacity } from 'react-native';

interface Boat {
  id: string;
  name: string;
  price: number;
}

const BoatList: React.FC<{ boats: Boat[] }> = ({ boats }) => {
  const renderBoat = ({ item }: { item: Boat }) => (
    <TouchableOpacity style={{ padding: 16 }}>
      <Text>{item.name}</Text>
      <Text>${item.price}/día</Text>
    </TouchableOpacity>
  );

  return (
    <FlatList
      data={boats}
      renderItem={renderBoat}
      keyExtractor={item => item.id}
    />
  );
};

export default BoatList;
`;

    // Crear o obtener experimento
    const experimentId = await agent.createOrGetExperiment();

    // Ejecutar análisis
    const analysis = await agent.runAnalysis(sampleCode, experimentId);

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
      analysis.suggestions.forEach(suggestion => console.log(`   ${suggestion}`));
    }

    // Guardar resultados
    const outputPath = path.join(__dirname, "analysis-result.json");
    await fs.writeFile(outputPath, JSON.stringify(analysis, null, 2));
    console.log(`\n💾 Resultado guardado en: ${outputPath}`);

  } catch (error) {
    console.error("\n❌ Error fatal:", error.message);
    process.exit(1);
  }
}

// Ejecutar si es el módulo principal
if (import.meta.url === `file://${process.argv[1]}`) {
  main();
}

export default Agent975AzureML;