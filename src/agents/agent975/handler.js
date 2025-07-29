module.exports = async function (context) {
  const code = context.request.body?.input?.code;

  if (!code) {
    return {
      statusCode: 400,
      body: {
        error: "Missing input.code field in request body"
      }
    };
  }

  const lines = code.split('\n').length;
  const size = code.length;
  const complexity = calculateComplexity(code);

  return {
    analysis: {
      lines,
      size,
      complexity,
      feedback: [
        "✅ Código recibido correctamente.",
        "📏 Líneas de código: " + lines,
        "📦 Tamaño (caracteres): " + size,
        "🔍 Complejidad ciclomática: " + complexity,
        complexity > 15 ? "⚠️ Alta complejidad detectada - se recomienda refactorización" : "✅ Complejidad aceptable"
      ]
    }
  };
};

function calculateComplexity(code) {
  // Simplified cyclomatic complexity calculation
  let complexity = 1;
  const patterns = [/if\s*\(/g, /else\s+if\s*\(/g, /for\s*\(/g, /while\s*\(/g, /case\s+/g, /\?\s*.*\s*:/g];
  
  patterns.forEach(pattern => {
    const matches = code.match(pattern);
    if (matches) complexity += matches.length;
  });
  
  return complexity;
}
