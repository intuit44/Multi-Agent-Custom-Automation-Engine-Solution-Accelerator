#!/usr/bin/env bash
# sync_from_upstream.sh
# Syncs safe upstream files (CI/CD, infra, docs) while protecting all src/ customizations.
# Usage: bash scripts/sync_from_upstream.sh

set -euo pipefail

UPSTREAM_REMOTE="upstream"
UPSTREAM_BRANCH="main"

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "❌ Working tree is not clean. Commit or stash changes before syncing."
  exit 1
fi

# Save current HEAD before any checkouts
ORIGINAL_HEAD=$(git rev-parse HEAD)

echo "📥 Fetching upstream..."
git fetch "$UPSTREAM_REMOTE"

# Files/dirs safe to sync freely from upstream (no custom logic)
SAFE_PATHS=(
  ".github/workflows"
  "infra/"
  "scripts/"
  "docs/"
  "data/"
  "azure.yaml"
  "README.md"
  ".gitignore"
  ".flake8"
)

# Capture protected files before syncing to avoid clobbering custom app logic.
mapfile -t PROTECTED_FILES < <(
  git diff "$UPSTREAM_REMOTE/$UPSTREAM_BRANCH".."$ORIGINAL_HEAD" --name-only --diff-filter=AMR | grep "^src/" || true
)

echo "🔄 Syncing safe paths from upstream..."
for path in "${SAFE_PATHS[@]}"; do
  git checkout "$UPSTREAM_REMOTE/$UPSTREAM_BRANCH" -- "$path" 2>/dev/null && echo "  ✓ $path" || true
done

# Clean up trailing spaces and ensure newlines at EOF (upstream files often have lint issues)
echo "🧹 Cleaning up lint issues (trailing spaces, EOF newlines)..."
# Remove trailing spaces from all YAML files in synced paths
find .github/workflows infra docs -type f \( -name "*.yml" -o -name "*.yaml" \) -exec sed -i 's/[[:space:]]*$//' {} \; 2>/dev/null || true
sed -i 's/[[:space:]]*$//' azure.yaml 2>/dev/null || true
# Ensure newline at EOF for all YAML files
find .github/workflows infra docs -type f \( -name "*.yml" -o -name "*.yaml" \) -print0 2>/dev/null | while IFS= read -r -d '' f; do
  [ -n "$(tail -c1 "$f" 2>/dev/null)" ] && echo "" >> "$f"
done
[ -f "azure.yaml" ] && [ -n "$(tail -c1 "azure.yaml" 2>/dev/null)" ] && echo "" >> "azure.yaml"
echo "  ✓ Lint cleanup complete"

# Restore ALL src/ customizations — never overwrite with upstream
if [ ${#PROTECTED_FILES[@]} -gt 0 ]; then
  echo "🛡️ Restoring protected src/ files..."
  for f in "${PROTECTED_FILES[@]}"; do
    git checkout "$ORIGINAL_HEAD" -- "$f" 2>/dev/null && echo "  ✓ $f" || true
  done
fi

echo ""
echo "✅ Sync complete."
echo ""
echo "Protected files (${#PROTECTED_FILES[@]}):"
printf '  %s\n' "${PROTECTED_FILES[@]:-<none>}"
echo ""
echo "Review changes with:"
echo "  git status"
echo "  git diff --staged"
