#!/bin/sh
# setupEnv.sh — Dev container post-create setup
# Each Python project gets its own .venv to avoid dependency collisions.
# Root .venv is a symlink → src/backend/.venv so VS Code & pytest "just work".
#
# NOTE: We intentionally do NOT use `set -e` here.
# Steps like git-pull (dirty tree), playwright install (network flaky), and
# npm install can fail transiently without blocking the rest of the setup.
# Each critical step checks its own exit status instead.

WS="/workspaces/Multi-Agent-Custom-Automation-Engine-Solution-Accelerator"

# ── 0. Pull latest code ──────────────────────────────────────────────
echo "📥 Pull latest code for the current branch"
cd "$WS"
if git diff --quiet && git diff --cached --quiet; then
  # Working tree is clean — safe to pull
  git fetch && git pull || echo "⚠️  git pull failed (merge conflict?). Continuing with current code."
else
  # Dirty working tree — stash, pull, pop
  echo "   Stashing local changes before pull…"
  git stash --include-untracked
  git fetch && git pull || echo "⚠️  git pull failed. Continuing with current code."
  git stash pop || echo "⚠️  git stash pop had conflicts — resolve manually after setup."
fi

# ── 1. Backend .venv (src/backend/.venv) ─────────────────────────────
echo "🔧 Setting up Backend (.venv)..."
cd "$WS/src/backend"
if ! uv sync --frozen; then
  echo "❌ Backend venv setup FAILED — check pyproject.toml / uv.lock"
  exit 1
fi
echo "   ✅ Backend: $(uv pip list 2>/dev/null | wc -l) packages"

# ── 2. MCP Server .venv (src/mcp_server/.venv) ──────────────────────
echo "🔧 Setting up MCP Server (.venv)..."
cd "$WS/src/mcp_server"
if ! uv sync --frozen --extra dev; then
  echo "❌ MCP Server venv setup FAILED — check pyproject.toml / uv.lock"
  exit 1
fi
echo "   ✅ MCP Server: $(uv pip list 2>/dev/null | wc -l) packages"

# ── 3. E2E test .venv (tests/e2e-test/.venv) ────────────────────────
echo "🔧 Setting up E2E tests (.venv)..."
cd "$WS/tests/e2e-test"
if ! uv venv --allow-existing .venv; then
  echo "❌ E2E venv creation FAILED"
  exit 1
fi
if ! VIRTUAL_ENV="$WS/tests/e2e-test/.venv" uv pip install -r requirements.txt; then
  echo "❌ E2E pip install FAILED — check requirements.txt"
  exit 1
fi
# Install Playwright browser (Chromium only to keep image small)
# Retry up to 3 times — apt/network can be flaky in containers
PW_OK=0
for attempt in 1 2 3; do
  echo "   Playwright install attempt $attempt/3…"
  if .venv/bin/python -m playwright install --with-deps chromium; then
    PW_OK=1
    break
  fi
  echo "   ⚠️  Attempt $attempt failed. Waiting 5s before retry…"
  sleep 5
done
if [ "$PW_OK" = "0" ]; then
  echo "   ⚠️  Playwright install failed after 3 attempts. E2E tests won't run until you manually run:"
  echo "       cd tests/e2e-test && .venv/bin/python -m playwright install --with-deps chromium"
fi
echo "   ✅ E2E: $(VIRTUAL_ENV=.venv uv pip list 2>/dev/null | wc -l) packages"

# ── 4. Symlink root .venv → backend (VS Code + pytest default) ──────
echo "🔗 Linking root .venv → src/backend/.venv"
cd "$WS"
rm -rf .venv 2>/dev/null || true
ln -sfn src/backend/.venv .venv

# ── 5. Frontend ─────────────────────────────────────────────────────
echo "🔧 Setting up Frontend..."
cd "$WS/src/frontend"
if ! npm install; then
  echo "⚠️  npm install failed — frontend won't work until you run 'npm install' in src/frontend"
fi
if [ -f requirements.txt ]; then
  pip install -r requirements.txt || echo "⚠️  pip install for frontend extras failed"
fi

# ── 6. Ensure gh CLI is available ───────────────────────────────────
if ! command -v gh >/dev/null 2>&1; then
  echo "📦 Installing GitHub CLI..."
  (type -p wget >/dev/null || (sudo apt-get update && sudo apt-get install wget -y)) \
    && sudo mkdir -p -m 755 /etc/apt/keyrings \
    && wget -nv -O- https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null \
    && sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null \
    && sudo apt-get update && sudo apt-get install gh -y
  echo "   ✅ gh $(gh --version | head -1)"
else
  echo "   ✅ gh already installed: $(gh --version | head -1)"
fi

echo ""
echo "🎉 Setup complete!"
echo "   Backend  → src/backend/.venv"
echo "   MCP      → src/mcp_server/.venv"
echo "   E2E      → tests/e2e-test/.venv"
echo "   Root     → .venv (symlink → backend)"
echo ""
