module.exports = async function (context, req) {
  context.log("Processing request for code analysis");
  const code = req.body?.input?.code;

  if (!code) {
    context.log("No code provided in request body");
    context.res = {
      status: 400,
      body: {
        error: "Missing input.code field in request body",
      },
    };
    return;
  }

  const lines = code.split("\n").length;
  const size = code.length;
  context.log(`Code has ${lines} lines and ${size} characters`);

  context.res = {
    status: 200,
    body: {
      analysis: {
        lines,
        size,
        feedback: [
          "✅ Código recibido correctamente.",
          "📏 Líneas de código: " + lines,
          "📦 Tamaño (caracteres): " + size,
        ],
      },
    },
  };
};

