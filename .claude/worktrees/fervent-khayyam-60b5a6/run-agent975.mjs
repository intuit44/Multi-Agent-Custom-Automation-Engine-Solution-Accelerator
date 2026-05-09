// run-agent975.mjs - Versión corregida para Azure AI Foundry
import { AIProjectClient } from "@azure/ai-projects";
import { DefaultAzureCredential } from "@azure/identity";
import { config } from "dotenv";
import fs from "fs/promises";
import path from "path";
import { fileURLToPath } from "url";

// Configurar rutas
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Cargar variables de entorno
config({ path: path.resolve(__dirname, "../../../.env") });

class Agent975Runner {
  constructor() {
    this.contextPath = path.resolve(__dirname, "../../../.codegpt/agents.context.json");
    this.config = null;
    this.client = null;
    this.debug = process.env.DEBUG === 'true';
  }

  log(message, level = 'info') {
    const prefix = {
      'info': '📋',
      'success': '✅',
      'error': '❌',
      'debug': '🔍',
      'warning': '⚠️'
    };

    if (level === 'debug' && !this.debug) return;

    console.log(`${prefix[level] || '•'} ${message}`);
  }

  async loadConfig() {
    try {
      this.log("Cargando configuración...", 'debug');

      const contextContent = await fs.readFile(this.contextPath, "utf-8");
      this.config = JSON.parse(contextContent);

      // Reemplazar variables de entorno si existen
      this.config.endpoint = process.env.AZURE_AI_FOUNDRY_ENDPOINT || this.config.endpoint;
      this.config.project = process.env.AZURE_AI_FOUNDRY_PROJECT || this.config.project;
      this.config.agentId = process.env.AGENT975_ID || this.config.agentId;

      // Validar configuración mínima
      if (!this.config.endpoint || !this.config.project || !this.config.agentId) {
        throw new Error("Faltan campos requeridos en la configuración");
      }

      this.log("Configuración cargada correctamente", 'success');

    } catch (error) {
      this.log(`Error al cargar configuración: ${error.message}`, 'error');
      throw error;
    }
  }

  async initialize() {
    try {
      await this.loadConfig();

      // Inicializar cliente con credenciales
      const credential = new DefaultAzureCredential();

      // Construir la URL base correcta
      const baseUrl = this.config.endpoint.replace(/\/+$/, ''); // Eliminar trailing slashes

      this.client = new AIProjectClient(baseUrl, credential);

      this.log("Agent975 inicializado correctamente", 'success');
      this.log(`Endpoint: ${baseUrl}`, 'info');
      this.log(`Proyecto: ${this.config.project}`, 'info');
      this.log(`Agent ID: ${this.config.agentId}`, 'info');

    } catch (error) {
      this.log(`Error al inicializar: ${error.message}`, 'error');
      throw error;
    }
  }

  async runConversation(message = "Analiza este código TSX para reservas de botes") {
    try {
      this.log("Iniciando conversación con el agente...", 'info');

      // Verificar que el agente existe
      let agent;
      try {
        agent = await this.client.agents.getAgent(this.config.agentId);
        this.log(`Agente encontrado: ${agent.name || this.config.agentId}`, 'success');
      } catch (error) {
        this.log(`Error al obtener agente: ${error.message}`, 'error');
        throw new Error(`No se pudo encontrar el agente ${this.config.agentId}`);
      }

      // Crear un thread para la conversación
      const thread = await this.client.agents.createThread();
      this.log(`Thread creado: ${thread.id}`, 'debug');

      // Agregar mensaje del usuario
      await this.client.agents.createMessage(thread.id, {
        role: "user",
        content: message
      });

      // Crear y ejecutar el run
      this.log("Ejecutando el agente...", 'info');
      const run = await this.client.agents.createRun(thread.id, {
        assistantId: this.config.agentId
      });

      // Esperar a que termine el run
      let runStatus = await this.waitForRunCompletion(thread.id, run.id);

      if (runStatus.status === 'completed') {
        // Obtener y mostrar los mensajes
        const messages = await this.client.agents.listMessages(thread.id);
        return this.processMessages(messages);
      } else {
        throw new Error(`Run terminó con estado: ${runStatus.status}`);
      }

    } catch (error) {
      this.log(`Error en la conversación: ${error.message}`, 'error');
      throw error;
    }
  }

  async waitForRunCompletion(threadId, runId, maxAttempts = 30) {
    let attempts = 0;

    while (attempts < maxAttempts) {
      const run = await this.client.agents.getRun(threadId, runId);

      this.log(`Estado del run: ${run.status}`, 'debug');

      if (['completed', 'failed', 'cancelled', 'expired'].includes(run.status)) {
        return run;
      }

      await new Promise(resolve => setTimeout(resolve, 2000));
      attempts++;
    }

    throw new Error("Timeout esperando que termine el run");
  }

  processMessages(messages) {
    const conversation = [];

    this.log("\nConversación completa:", 'info');
    this.log("─".repeat(50), 'info');

    for (const message of messages.data.reverse()) {
      const content = message.content
        .filter(c => c.type === 'text')
        .map(c => c.text.value)
        .join('\n');

      conversation.push({
        role: message.role,
        content: content
      });

      console.log(`\n${message.role.toUpperCase()}:`);
      console.log(content);
    }

    this.log("─".repeat(50), 'info');

    return conversation;
  }

  // Método alternativo usando invoke directo (si está disponible)
  async analyzeCodeDirect(code) {
    try {
      this.log("Analizando código TSX...", 'info');

      const response = await this.client.agents.invoke(
        this.config.project,
        this.config.agentId,
        {
          input: { code },
          parameters: {
            temperature: 0.3,
            max_tokens: 2000
          }
        }
      );

      return this.processDirectResponse(response);

    } catch (error) {
      this.log(`Error en análisis directo: ${error.message}`, 'error');

      // Si falla el método directo, intentar con conversación
      this.log("Intentando método de conversación...", 'warning');
      const codeMessage = `Analiza el siguiente código TSX:\n\n\`\`\`tsx\n${code}\n\`\`\``;
      return await this.runConversation(codeMessage);
    }
  }

  processDirectResponse(response) {
    if (!response || !response.body) {
      throw new Error("Respuesta vacía del agente");
    }

    const result = {
      status: "success",
      timestamp: new Date().toISOString(),
      analysis: response.body.analysis || {},
      metrics: response.body.metrics || {},
      suggestions: response.body.suggestions || []
    };

    this.log("Análisis completado", 'success');

    if (result.analysis.feedback) {
      console.log("\n📊 Feedback:");
      result.analysis.feedback.forEach(item => console.log(`   ${item}`));
    }

    return result;
  }
}

// Función principal
async function main() {
  const runner = new Agent975Runner();

  try {
    await runner.initialize();

    // Código de ejemplo para analizar
    const sampleCode = `
import React from 'react';
import { FlatList, View, Text, TouchableOpacity } from 'react-native';

interface Boat {
  id: string;
  name: string;
  price: number;
  capacity: number;
}

interface BoatListProps {
  boats: Boat[];
  onSelectBoat: (boat: Boat) => void;
}

const BoatList: React.FC<BoatListProps> = ({ boats, onSelectBoat }) => {
  const renderBoat = ({ item }: { item: Boat }) => (
    <TouchableOpacity 
      style={{ padding: 16, borderBottomWidth: 1, borderBottomColor: '#eee' }}
      onPress={() => onSelectBoat(item)}
    >
      <Text style={{ fontSize: 18, fontWeight: 'bold' }}>{item.name}</Text>
      <Text style={{ fontSize: 14, color: '#666' }}>
        Precio: ${item.price}/día - Capacidad: {item.capacity} personas
      </Text>
    </TouchableOpacity>
  );

  return (
    <FlatList
      data={boats}
      renderItem={renderBoat}
      keyExtractor={item => item.id}
      contentContainerStyle={{ paddingVertical: 8 }}
    />
  );
};

export default BoatList;
`;

    // Verificar argumentos de línea de comandos
    const args = process.argv.slice(2);

    if (args.includes('--test')) {
      // Modo de prueba simple
      console.log("\n🧪 Ejecutando prueba de conexión...");
      await runner.runConversation("Hola, ¿estás funcionando correctamente?");

    } else if (args.includes('--direct')) {
      // Usar método directo
      await runner.analyzeCodeDirect(sampleCode);

    } else {
      // Usar método de conversación por defecto
      const message = `Por favor analiza el siguiente código TSX y proporciona:
1. Un resumen del componente
2. Posibles mejoras de rendimiento
3. Sugerencias de accesibilidad
4. Mejores prácticas de React Native

Código:
\`\`\`tsx
${sampleCode}
\`\`\``;

      await runner.runConversation(message);
    }

    console.log("\n✅ Proceso completado exitosamente");

  } catch (error) {
    console.error("\n❌ Error fatal:", error.message);
    if (runner.debug) {
      console.error("Stack trace:", error.stack);
    }
    process.exit(1);
  }
}

// Ejecutar si es el módulo principal
if (import.meta.url === `file://${process.argv[1]}`) {
  main();
}

export default Agent975Runner;