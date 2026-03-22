#!/usr/bin/env bash
# =============================================================================
# FPL Bot — Hetzner VPS one-command setup
#
# Run this on a fresh Hetzner VPS (Ubuntu 22.04 LTS recommended):
#   curl -sSL https://raw.githubusercontent.com/YOUR_USER/Fantasy-football/main/scripts/setup-vps.sh | bash
#
# Or copy the repo first then run:
#   bash scripts/setup-vps.sh
#
# What this does:
#   1. Updates the system
#   2. Installs Docker + Docker Compose
#   3. Hardens SSH (disables password auth)
#   4. Configures UFW firewall
#   5. Sets up automatic unattended security updates
#   6. Creates a daily DB backup cron job
#   7. Guides you through .env setup
# =============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Colour

info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

REPO_DIR="${REPO_DIR:-/opt/fpl-bot}"

# ── 1. System update ──────────────────────────────────────────────────────────
info "Updating system packages..."
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -qq
success "System updated"

# ── 2. Install Docker ─────────────────────────────────────────────────────────
if command -v docker &>/dev/null; then
    success "Docker already installed ($(docker --version))"
else
    info "Installing Docker..."
    apt-get install -y -qq ca-certificates curl gnupg lsb-release

    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg

    echo \
        "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
        https://download.docker.com/linux/ubuntu \
        $(lsb_release -cs) stable" \
        > /etc/apt/sources.list.d/docker.list

    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
    systemctl enable --now docker
    success "Docker installed"
fi

# Docker Compose v2 (plugin)
if ! docker compose version &>/dev/null; then
    error "docker compose plugin not found — check Docker installation"
fi
success "Docker Compose available ($(docker compose version --short))"

# ── 3. Firewall ───────────────────────────────────────────────────────────────
info "Configuring UFW firewall..."
apt-get install -y -qq ufw
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw --force enable
success "Firewall configured (SSH only inbound)"

# ── 4. Unattended security upgrades ──────────────────────────────────────────
info "Enabling automatic security updates..."
apt-get install -y -qq unattended-upgrades
echo 'APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";' > /etc/apt/apt.conf.d/20auto-upgrades
success "Automatic security updates enabled"

# ── 5. Clone / update repo ────────────────────────────────────────────────────
if [[ -d "$REPO_DIR/.git" ]]; then
    info "Updating existing repo at $REPO_DIR..."
    git -C "$REPO_DIR" pull --ff-only
else
    info "Cloning repository to $REPO_DIR..."
    echo ""
    echo -e "${YELLOW}Enter your GitHub repository URL (e.g. https://github.com/xvisher/Fantasy-football.git):${NC}"
    read -r REPO_URL
    git clone "$REPO_URL" "$REPO_DIR"
fi
success "Repository ready at $REPO_DIR"

# ── 6. .env setup ─────────────────────────────────────────────────────────────
if [[ ! -f "$REPO_DIR/.env" ]]; then
    cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
    echo ""
    echo -e "${YELLOW}═══════════════════════════════════════════════════${NC}"
    echo -e "${YELLOW}  Configure your FPL credentials in .env${NC}"
    echo -e "${YELLOW}═══════════════════════════════════════════════════${NC}"
    echo ""
    echo -e "${YELLOW}FPL Email (premierleague.com login):${NC}"
    read -r FPL_EMAIL
    echo -e "${YELLOW}FPL Password:${NC}"
    read -rs FPL_PASSWORD
    echo ""
    echo -e "${YELLOW}FPL Team ID (from your FPL URL, e.g. /entry/1234567/):${NC}"
    read -r FPL_TEAM_ID
    echo -e "${YELLOW}Discord Webhook URL (leave blank to skip):${NC}"
    read -r DISCORD_URL

    sed -i "s|FPL_EMAIL=.*|FPL_EMAIL=${FPL_EMAIL}|" "$REPO_DIR/.env"
    sed -i "s|FPL_PASSWORD=.*|FPL_PASSWORD=${FPL_PASSWORD}|" "$REPO_DIR/.env"
    sed -i "s|FPL_TEAM_ID=.*|FPL_TEAM_ID=${FPL_TEAM_ID}|" "$REPO_DIR/.env"
    if [[ -n "$DISCORD_URL" ]]; then
        sed -i "s|DISCORD_WEBHOOK_URL=.*|DISCORD_WEBHOOK_URL=${DISCORD_URL}|" "$REPO_DIR/.env"
    fi
    # Start in dry-run mode for first boot — let the user verify before going live
    sed -i "s|DRY_RUN=.*|DRY_RUN=true|" "$REPO_DIR/.env"
    success ".env configured (DRY_RUN=true — bot will analyse but not submit transfers)"
else
    success ".env already exists — skipping credential setup"
fi

# ── 7. Daily DB backup cron ───────────────────────────────────────────────────
info "Setting up daily database backup..."
BACKUP_DIR="/opt/fpl-bot-backups"
mkdir -p "$BACKUP_DIR"
cat > /etc/cron.daily/fpl-bot-backup << 'CRON'
#!/bin/bash
BACKUP_DIR=/opt/fpl-bot-backups
VOLUME=$(docker volume ls -q | grep fpl-data || true)
if [[ -n "$VOLUME" ]]; then
    docker run --rm \
        -v "$VOLUME:/data:ro" \
        -v "$BACKUP_DIR:/backup" \
        alpine \
        cp /data/fpl_bot.db "/backup/fpl_bot_$(date +%Y%m%d).db"
    # Keep last 30 days
    find "$BACKUP_DIR" -name "fpl_bot_*.db" -mtime +30 -delete
fi
CRON
chmod +x /etc/cron.daily/fpl-bot-backup
success "Daily DB backup scheduled → $BACKUP_DIR"

# ── 8. Build and start ────────────────────────────────────────────────────────
info "Building Docker image..."
docker compose -f "$REPO_DIR/docker-compose.yml" build

info "Starting FPL bot..."
docker compose -f "$REPO_DIR/docker-compose.yml" up -d

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  FPL Bot is running!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo ""
echo "  Useful commands (run from $REPO_DIR):"
echo ""
echo "  make logs          → live log stream"
echo "  make status        → show bot status + next scheduled GW job"
echo "  make dry-run GW=1  → test GW1 analysis without submitting"
echo "  make go-live       → disable dry-run and restart"
echo "  make update        → pull latest code and redeploy"
echo "  make backup        → manual DB backup"
echo ""
echo -e "  ${YELLOW}Bot is in DRY_RUN mode.${NC} When you're happy with the decisions:"
echo -e "  ${YELLOW}  cd $REPO_DIR && make go-live${NC}"
echo ""
