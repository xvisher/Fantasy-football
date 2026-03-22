# FPL Bot — convenience commands
# Run from the repo root on your Hetzner VPS (or local machine).
#
# ── LOCAL DASHBOARD (your PC) ─────────────────────────────────────────────────
#   make local-up         → start local dashboard at http://localhost:8080
#   make local-down       → stop local dashboard
#   make local-logs       → live log stream for local dashboard
#   make local-build      → rebuild local image (after requirements change)
#
# ── HETZNER BOT (VPS) ────────────────────────────────────────────────────────
#   make logs             → live log stream (Ctrl+C to exit)
#   make status           → container status + next scheduled jobs
#   make dry-run GW=5     → analyse GW5, no transfers submitted
#   make run GW=5         → run GW5 cycle for real (uses .env DRY_RUN setting)
#   make post-gw GW=4     → trigger post-GW learning for GW4
#   make go-live          → disable dry-run and restart
#   make pause            → enable dry-run (pause auto-transfers)
#   make update           → pull latest code and redeploy
#   make restart          → restart container (picks up .env changes)
#   make backup           → manual DB backup to ./backups/
#   make decisions        → show last 20 decisions from audit log
#   make weights          → show current learned model weights
#   make shell            → open shell inside running container
#   make stop             → stop the bot
#   make start            → start the bot

COMPOSE       := docker compose
COMPOSE_LOCAL := docker compose -f docker-compose.local.yml
BOT           := fpl-bot
DASHBOARD     := fpl-dashboard

.PHONY: logs status dry-run run post-gw go-live pause update restart backup decisions weights shell stop start build \
        local-up local-down local-logs local-build

# ── Local dashboard ───────────────────────────────────────────────────────────

local-up:
	@if [ ! -f .env.local ]; then \
	  cp .env.local.example .env.local; \
	  echo "Created .env.local from example — edit it with your FPL credentials first!"; \
	  echo "  nano .env.local"; \
	  exit 1; \
	fi
	$(COMPOSE_LOCAL) up -d
	@echo "Dashboard running at http://localhost:8080"

local-down:
	$(COMPOSE_LOCAL) down

local-logs:
	$(COMPOSE_LOCAL) logs -f --tail 100 $(DASHBOARD)

local-build:
	$(COMPOSE_LOCAL) build --no-cache

logs:
	$(COMPOSE) logs -f --tail 100 $(BOT)

status:
	@echo "=== Container status ==="
	$(COMPOSE) ps
	@echo ""
	@echo "=== Resource usage ==="
	docker stats --no-stream $(BOT) 2>/dev/null || true
	@echo ""
	@echo "=== Scheduled jobs (next 5) ==="
	$(COMPOSE) exec $(BOT) python -c "\
import asyncio, sys; \
sys.path.insert(0, '.'); \
from src.data.database import get_upcoming_deadlines, init_db; \
async def show(): \
    await init_db(); \
    rows = await get_upcoming_deadlines(); \
    [print(f'  GW{r[\"id\"]:>2}  {r[\"deadline_time\"]}') for r in rows[:5]]; \
asyncio.run(show())" 2>/dev/null || echo "  (bot not running)"

dry-run:
ifndef GW
	$(error GW is not set. Usage: make dry-run GW=5)
endif
	@echo "Running GW$(GW) in dry-run mode (no transfers will be submitted)..."
	$(COMPOSE) exec $(BOT) python main.py --once $(GW) --dry-run

run:
ifndef GW
	$(error GW is not set. Usage: make run GW=5)
endif
	@echo "Running GW$(GW) cycle (using DRY_RUN setting from .env)..."
	$(COMPOSE) exec $(BOT) python main.py --once $(GW)

post-gw:
ifndef GW
	$(error GW is not set. Usage: make post-gw GW=4)
endif
	@echo "Running post-GW$(GW) update (score fetch + learning)..."
	$(COMPOSE) exec $(BOT) python main.py --post-gw $(GW)

go-live:
	@echo "Disabling dry-run mode and restarting..."
	sed -i 's/^DRY_RUN=.*/DRY_RUN=false/' .env
	$(COMPOSE) restart $(BOT)
	@echo "Bot is now LIVE. Transfers will be submitted automatically."

pause:
	@echo "Enabling dry-run mode (bot will analyse but not submit transfers)..."
	sed -i 's/^DRY_RUN=.*/DRY_RUN=true/' .env
	$(COMPOSE) restart $(BOT)
	@echo "Bot paused. Use 'make go-live' to re-enable."

build:
	$(COMPOSE) build

update:
	@echo "Pulling latest code and redeploying..."
	bash scripts/deploy.sh

restart:
	$(COMPOSE) restart $(BOT)
	@echo "Bot restarted."

backup:
	bash scripts/backup-db.sh

decisions:
	@echo "=== Last 20 decisions ==="
	$(COMPOSE) exec $(BOT) python -c "\
import asyncio, sys; \
sys.path.insert(0, '.'); \
from src.data.database import get_decisions_for_learner, init_db; \
async def show(): \
    await init_db(); \
    rows = await get_decisions_for_learner(38); \
    rows = rows[:20]; \
    print(f'  {'GW':<4} {'Type':<16} {'Predicted xP':<14} {'Actual pts':<12} {'Dry?'}'); \
    print('  ' + '-'*60); \
    [print(f'  {r[\"gw\"]:<4} {r[\"action_type\"]:<16} {r[\"predicted_xp\"]:<14.1f} {str(r[\"actual_pts\"] or \"pending\"):<12} {\"[DRY]\" if r.get(\"dry_run\") else \"\"}') for r in rows]; \
asyncio.run(show())" 2>/dev/null

weights:
	@echo "=== Current model weights ==="
	$(COMPOSE) exec $(BOT) python -c "\
import asyncio, sys; \
sys.path.insert(0, '.'); \
from src.data.database import get_latest_model_weights, init_db; \
async def show(): \
    await init_db(); \
    w = await get_latest_model_weights(); \
    if w: \
        [print(f'  {k}: {v:.4f}') for k, v in w.items()]; \
    else: \
        print('  No learned weights yet (using defaults from settings.py)'); \
asyncio.run(show())" 2>/dev/null

shell:
	$(COMPOSE) exec $(BOT) /bin/bash

stop:
	$(COMPOSE) stop $(BOT)
	@echo "Bot stopped."

start:
	$(COMPOSE) up -d $(BOT)
	@echo "Bot started."
