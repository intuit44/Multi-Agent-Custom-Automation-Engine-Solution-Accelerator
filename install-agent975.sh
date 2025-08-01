#!/bin/bash
echo "🚀 Instalando Agent975..."

# Mover agents.context.json si está en la raíz
if [ -f "agents.context.json" ]; then
  echo "📦 Moviendo agents.context.json a .codegpt/"
  mv agents.context.json .codegpt/
fi

# Instalar dependencias del agente
cd src/agents/agent975
npm install

echo "✅ Agent975 instalado correctamente"