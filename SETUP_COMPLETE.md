# 📋 Resumen: Sistema de Seguridad de Comandos

## ¿Qué Se Agregó?

### 1. **MCP Shell Commander** (En `mcp.json`)
   - Permite ejecutar comandos shell permitidos (grep, pytest, npm, docker, curl, git, etc.)
   - Whitelist de ~50 comandos seguros
   - Integrado en VS Code/Agent como MCP server

### 2. **MCP Command Validator** (Python)
   - Archivo: `mcp_command_validator.py`
   - Detecta automáticamente comandos peligrosos:
     - Logs sin truncación (`docker logs` → `docker logs --tail 100`)
     - Servicios sin detached (`docker run` → `docker run -d`)
     - Curl sin timeout (`curl` → `curl -m 10`)
     - Tests con watch infinito
   - **Auto-fix activado**: Aplica correcciones automáticamente

### 3. **Safe Command Wrapper** (Bash)
   - Archivo: `safe-cmd.sh` (ejecutable)
   - Wrapper bash que valida antes de ejecutar
   - Uso: `./safe-cmd.sh 'tu-comando'`
   - Agrega timeout de 300s automáticamente

### 4. **Documentación Completa**
   - `COMMAND_SAFETY_GUIDE.md` ← **LEER PRIMERO**
   - `COMMAND_EXAMPLES.md` → Casos reales para MACAE
   - `COMMAND_SAFETY_README.md` → Configuración y extensión

---

## 🎯 Cómo Funciona en Práctica

### Escenario 1: Agent Ejecuta `docker logs myapp`
```
Agent: "Ejecuta docker logs myapp"
  ↓
shell-commander recibe comando
  ↓
command-validator detecta: ⚠️ Sin truncación
  ↓
AUTO_FIX=true → Convierte a: docker logs --tail 100 myapp
  ↓
Ejecuta y retorna últimas 100 líneas (NO bloquea)
```

### Escenario 2: Agent Ejecuta `uvicorn app:app`
```
Agent: "Levanta uvicorn"
  ↓
command-validator detecta: ⚠️ Sin detached
  ↓
AUTO_FIX=true → Convierte a: uvicorn app:app &
  ↓
Ejecuta en background (NO ocupa terminal)
```

### Escenario 3: Agent Ejecuta `docker logs --tail 100 myapp`
```
Agent: "Ejecuta docker logs --tail 100 myapp"
  ↓
command-validator valida: ✅ SAFE (tiene --tail)
  ↓
Ejecuta directamente
```

---

## 📋 Conversión Rápida de Comandos Peligrosos

| Caso | ❌ NUNCA | ✅ SIEMPRE |
|------|---------|----------|
| Ver logs | `docker logs` | `docker logs --tail 100` |
| Seguir logs | `tail -f file` | `timeout 30 tail -f file` O `tail -n 100 file` |
| Lanzar container | `docker run img` | `docker run -d img` |
| Levantar dev | `npm run dev` | `npm run dev &` |
| Compilar backend | `uvicorn app:app` | `uvicorn app:app &` |
| HTTP request | `curl url` | `curl -m 10 url` |
| Test watch | `pytest --watch` | `pytest file.py` |

---

## 🔧 Configuración Mínima Requerida

### En `mcp.json` (YA HECHO ✅)
```json
{
  "shell-commander": { ... },
  "command-validator": { ... }
}
```

### En Instrucciones del Agente (RECOMENDADO)
Agrega a la sección de "system instructions":
```
# COMMAND EXECUTION RULES
1. Logs SIEMPRE con --tail/--lines/-n
2. Servicios SIEMPRE con -d o &
3. Curl SIEMPRE con -m (timeout)
4. Pytest NUNCA con --watch
5. Si en duda, usa 'docker logs --tail 50' como template
```

---

## 🧪 Validar que Funciona

```bash
# Test 1: Validador Python
python3 mcp_command_validator.py
# ✅ Debe mostrar comandos SAFE/UNSAFE

# Test 2: Wrapper Bash
./safe-cmd.sh 'echo test'
# ✅ Debe ejecutar y mostrar "test"

# Test 3: Comandos reales
./safe-cmd.sh 'ls -la src/frontend/'
./safe-cmd.sh 'grep -r "planId" src/frontend/src/pages/'
```

---

## 📚 Archivos Creados

```
/workspaces/Multi-Agent-Custom-Automation-Engine-Solution-Accelerator/
├── mcp_command_validator.py          (Python validator - MCP server)
├── safe-cmd.sh                       (Bash wrapper - ejecutable)
├── COMMAND_SAFETY_README.md          (← Este archivo)
├── COMMAND_SAFETY_GUIDE.md           (Guía detallada)
└── COMMAND_EXAMPLES.md               (Ejemplos prácticos MACAE)

/home/vscode/.aitk/
└── mcp.json                          (Configuración MCP actualizada)
```

---

## 🚀 Próximos Pasos

1. **Leer** `COMMAND_SAFETY_GUIDE.md` para entender todas las reglas
2. **Ver** `COMMAND_EXAMPLES.md` para casos reales
3. **Testear** con `./safe-cmd.sh 'tu-comando'`
4. **Instruir al agente** con las reglas de command execution
5. **Monitorear** logs en tiempo real de forma segura

---

## 🎓 Lecciones Clave

✅ **Después de esta configuración, el agente:**
- No puede ejecutar `docker logs` sin truncación
- No puede levantar servicios que bloqueen
- No puede correr `pytest --watch` infinitamente
- Automáticamente aplica `--tail`, `-d`, `&` cuando falta
- Tiene timeout de 5 minutos en cualquier comando
- Limita output a 5000 caracteres

❌ **Antes, el agente:**
- Ejecutaba `docker logs` y se quedaba esperando
- Levantaba `npm run dev` sin `&` y bloqueaba terminal
- No tenía mecanismo de truncación automática

---

## 🔐 Seguridad

- Whitelist de comandos (no pueden ejecutar `rm -rf` directamente)
- Truncación automática (no pueden generar 1GB de output)
- Timeout automático (no pueden dejar procesos colgados)
- Auto-fix de patrones peligrosos (prevención activa)

---

## ❓ FAQ

**P: ¿Qué pasa si el agente intenta `docker logs myapp` sin `--tail`?**
R: El `command-validator` lo detecta, auto-lo corrige a `docker logs --tail 100 myapp`, y ejecuta la versión segura.

**P: ¿Puedo desactivar AUTO_FIX?**
R: Sí, en `mcp.json`: `"AUTO_FIX": "false"` hará que deniegue en lugar de auto-arreglar.

**P: ¿Cómo agrego un comando permitido?**
R: En `mcp.json`, edita `ALLOWED_COMMANDS` y agrega tu comando.

**P: ¿Cómo agrego una nueva regla de seguridad?**
R: En `mcp_command_validator.py`, edita `FOREGROUND_DANGER_PATTERNS` o `SERVICE_PATTERNS`.

**P: ¿Puedo ejecutar comandos interactivos (como `vim`)?**
R: No, no están en whitelist. Solo comandos no-interactivos.

---

## 📞 Contacto / Issues

Si el validador no funciona bien para tu caso:
1. Abre `mcp_command_validator.py`
2. Agrega patrón a `FOREGROUND_DANGER_PATTERNS` o `SERVICE_PATTERNS`
3. Testea con `python3 mcp_command_validator.py`
4. Reinicia VS Code para que recarga la configuración MCP

---

**Última actualización:** 2026-07-03
**Estado:** ✅ Operativo
