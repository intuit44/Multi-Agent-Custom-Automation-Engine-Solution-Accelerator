// run-agent975-debug.mjs - Versión con logging completo
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

class Agent975Debug {
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
      }

      // Cargar configuración
      const contextContent = await fs.readFile(this.contextPath, "utf-8");
      this.config = JSON.parse(contextContent);
      console.log("✅ Configuración cargada:");
      console.log(JSON.stringify(this.config, null, 2));

      // Verificar credenciales
      console.log("\n🔑 Verificando credenciales de Azure...");
      this.credential = new DefaultAzureCredential();

      try {
        const token = await this.credential.getToken("https://ml.azure.com/.default");
        console.log("✅ Token obtenido correctamente");
        console.log(`   Expira: ${new Date(token.expiresOnTimestamp).toLocaleString()}`);
      } catch (error) {
        console.error("❌ Error al obtener token:");
        console.error(`   ${error.message}`);
        console.log("\n💡 Sugerencias:");
        console.log("   1. Ejecuta: az login");
        console.log("   2. O configura las variables AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID");
        throw error;
      }

      console.log("\n✅ Agent975 inicializado correctamente");

    } catch (error) {
      console.error("\n❌ Error durante la inicialización:");
      console.error(error.message);
      throw error;
    }
  }

  async testMLflowConnection() {
    console.log("\n🧪 Probando conexión con MLflow...");

    try {
      const token = await this.credential.getToken("https://ml.azure.com/.default");
      const baseUrl = `${this.config.endpoint}/mlflow/v2.0/api/2.0/mlflow/experiments/search`;

      console.log(`📍 URL: ${baseUrl}`);

      const response = await fetch(baseUrl, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${token.token}`,
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          max_results: 1
        })
      });

      console.log(`📊 Respuesta: ${response.status} ${response.statusText}`);

      if (response.ok) {
        const data = await response.json();
        console.log("✅ Conexión exitosa con MLflow");
        if (data.experiments && data.experiments.length > 0) {
          console.log(`   Experimento encontrado: ${data.experiments[0].name}`);
        }
        return true;
      } else {
        const errorText = await response.text();
        console.error("❌ Error en la respuesta:");
        console.error(errorText);
        return false;
      }

    } catch (error) {
      console.error("❌ Error al conectar con MLflow:");
      console.error(error.message);
      return false;
    }
  }

  async analyzeCodeSimple(code) {
    console.log("\n📝 Ejecutando análisis simple del código...");

    const lines = code.split('\n').filter(line => line.trim()).length;
    const size = code.length;

    const analysis = {
      timestamp: new Date().toISOString(),
      lines,
      size,
      feedback: [
        `✅ Código recibido: ${size} caracteres`,
        `📏 Líneas de código: ${lines}`,
        `🔍 Análisis completado localmente`
      ]
    };

    console.log("\n📊 Resultados:");
    analysis.feedback.forEach(f => console.log(`   ${f}`));

    return analysis;
  }
}

// Función principal con manejo de errores mejorado
async function main() {
  console.log("═".repeat(60));
  console.log("        AGENT975 - AZURE ML INTEGRATION (DEBUG MODE)");
  console.log("═".repeat(60));

  const agent = new Agent975Debug();

  try {
    // Paso 1: Inicializar
    await agent.initialize();

    // Paso 2: Probar conexión
    const connected = await agent.testMLflowConnection();

    if (!connected) {
      console.log("\n⚠️  No se pudo conectar a MLflow, ejecutando análisis local...");
    }

    // Paso 3: Analizar código de ejemplo
    const sampleCode = `
const BoatList = ({ boats }) => (
  <FlatList
    data={boats}
    renderItem={({ item }) => <BoatCard boat={item} />}
    keyExtractor={item => item.id}
  />
);`;

    const analysis = await agent.analyzeCodeSimple(sampleCode);

    // Guardar resultado
    const outputPath = path.join(__dirname, "analysis-debug.json");
    await fs.writeFile(outputPath, JSON.stringify(analysis, null, 2));
    console.log(`\n💾 Resultado guardado en: ${outputPath}`);

    console.log("\n✅ Proceso completado exitosamente");

  } catch (error) {
    console.error("\n💥 ERROR FATAL:");
    console.error("Mensaje:", error.message);
    console.error("\nStack trace:");
    console.error(error.stack);

    console.log("\n📋 Información de debug:");
    console.log(`   Node version: ${process.version}`);
    console.log(`   Platform: ${process.platform}`);
    console.log(`   Current directory: ${process.cwd()}`);
    console.log(`   Script location: ${__dirname}`);

    process.exit(1);
  }
}

// Capturar errores no manejados
process.on('unhandledRejection', (reason, promise) => {
  console.error('\n💥 Unhandled Rejection at:', promise, 'reason:', reason);
  process.exit(1);
});

process.on('uncaughtException', (error) => {
  console.error('\n💥 Uncaught Exception:', error);
  process.exit(1);
});

// Verificar si es el módulo principal y ejecutar
if (import.meta.url === `file://${process.argv[1]}`) {
  console.log("🏁 Ejecutando como módulo principal...\n");
  main().catch(error => {
    console.error("\n💥 Error no capturado en main:");
    console.error(error);
    process.exit(1);
  });
} else {
  console.log("📦 Módulo cargado para importación");
}

export default Agent975Debug;