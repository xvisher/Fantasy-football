#!/usr/bin/env bash
# =============================================================================
# FPL Bot — deploy / update script
#
# Usage (from /opt/fpl-bot or any clone of the repo):
#   bash scripts/deploy.sh          # pull latest + rebuild + restart
#   bash scripts/deploy.sh --build  # force full image rebuild
# =============================================================================

set -euo pipefail

GREEN='\033[0;32m'; BLUE='\033[0;34m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${BLUE}[deploy]${NC} $*"; }
success() { echo -e "${GREEN}[deploy]${NC} $*"; }
warn()    { echo -e "${YELLOW}[deploy]${NC} $*"; }

FORCE_BUILD="${1:-}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

# ── 1. Pull latest code ───────────────────────────────────────────────────────
info "Pulling latest code..."
git fetch origin
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "@{u}" 2>/dev/null || echo "")

if [[ -z "$REMOTE" ]]; then
    warn "No upstream branch set — skipping pull"
elif [[ "$LOCAL" == "$REMOTE" ]]; then
    success "Already up to date"
else
    git pull --ff-only
    success "Updated $(git log --oneline -1)"
fi

# ── 2. Rebuild if needed ──────────────────────────────────────────────────────
if [[ "$FORCE_BUILD" == "--build" ]] || git diff --name-only HEAD~1 HEAD 2>/dev/null | grep -qE "requirements.txt|Dockerfile"; then
    info "Dependencies or Dockerfile changed — rebuilding image..."
    docker compose build --no-cache
else
    info "No dependency changes — using cached image"
    docker compose build
fi

# ── 3. Restart with zero-downtime (stop → up) ─────────────────────────────────
info "Restarting bot..."
docker compose down --timeout 30
docker compose up -d

success "Bot redeployed. Logs:"
docker compose logs --tail 20
