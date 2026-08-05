#!/usr/bin/env bash
# sync-mcp-configs.sh
# ─────────────────────────────────────────────────────────────────────
# Single source of truth → mcp-inspector-config.json
# Regenerates blackbox_mcp_settings.json from it (stripping "note" fields
# and any inspector-only metadata) so both stay in lock-step.
#
# Usage:  bash scripts/sync-mcp-configs.sh
# ─────────────────────────────────────────────────────────────────────

set -euo pipefail

WS="/workspaces/Multi-Agent-Custom-Automation-Engine-Solution-Accelerator"
SRC="$WS/mcp-inspector-config.json"
DST="$WS/blackbox_mcp_settings.json"

if [ ! -f "$SRC" ]; then
  echo "❌ Source not found: $SRC"
  exit 1
fi

# Validate source JSON
if ! python3 -c "import json,sys; json.load(open('$SRC'))" 2>/dev/null; then
  echo "❌ Invalid JSON in $SRC"
  exit 1
fi

# Strip "note" fields (Blackbox doesn't need them) and write to DST
python3 - << PYEOF
import json, sys

src = json.load(open("$SRC"))
out = {"mcpServers": {}}

for name, cfg in src.get("mcpServers", {}).items():
    clean = {k: v for k, v in cfg.items() if k != "note"}
    out["mcpServers"][name] = clean

with open("$DST", "w") as f:
    json.dump(out, f, indent=2)
    f.write("\n")

print(f"✅ Synced {len(out['mcpServers'])} MCP server(s):")
for name in out["mcpServers"].keys():
    print(f"   - {name}")
PYEOF

echo ""
echo "📂 Source:      $SRC"
echo "📂 Destination: $DST"
echo "✨ Done."
