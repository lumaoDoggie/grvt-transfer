# GRVT Rebalance & Transfer Bot

Language: English | 中文说明: `README.md`

Automated **equity rebalancing** and optional **emergency position unwinding** for two GRVT trading subaccounts. The loop monitors account equity/margin, transfers funds to keep accounts balanced, and can reduce positions (reduce-only market orders) when margin usage exceeds configured thresholds. Telegram is supported for alerts and a small status bot.

## What this project does
- Monitors **two trading subaccounts** (A/B): `total_equity`, `maintenance_margin`, `available_balance`
- **Rebalances** by transferring USDT so A/B stay roughly equal
- Optionally triggers **unwind** to reduce positions when margin usage is high
- Sends **Telegram alerts** + supports `/start` and `/view` status commands (restricted by `TELEGRAM_CHAT_ID`)

GRVT sites:
- Prod: `https://grvt.io`
- Testnet: `https://testnet.grvt.io`

## Safety (read before running)
- This repo can place orders and move funds. Review the code carefully before running.
- Keep `unwind.dryRun: true` until you are ready for live operation.
- Never commit secrets (`.env*` is ignored; use `.env.example` as a template).
- Telegram commands are **locked down**: if `TELEGRAM_CHAT_ID` is set, all other chats are ignored.

## Windows GUI (prod/test selectable)
- Run: `grvt-transfer gui` (or `python -m grvt_transfer gui`)
- Flow: fill credentials → click `验证` (sends a Telegram test message) → click `开始/停止`
- Settings file: `%APPDATA%\\grvt-transfer\\settings.json` (contains secrets)

Release zip: double-click `grvt-transfer-gui.exe` (or `scripts\\windows\\Start-GUI.bat`).

## Quick start (Windows PC, no Python needed)
1) Download `grvt-transfer-windows.zip` from GitHub Releases and unzip it.
2) Create `.env` from `.env.example`, then edit it:
   - `GRVT_ENV=test` (recommended first) or `prod`
   - `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
   - Account keys/secrets for both accounts
3) Edit thresholds in `config/test/*.yaml` (or `config/prod/*.yaml`).
4) Double-click `scripts/windows/Start.bat`.

Stop: double-click `scripts/windows/Stop.bat`.

Tip: to get your Telegram `chat_id`, message `@userinfobot`.

## Quick start (Linux VPS, Docker recommended)
Prereqs: Docker Engine + Docker Compose plugin installed.

```bash
git clone <your-repo-url>
cd grvt-transfer
cp .env.example .env
# edit .env + config/<env>/*.yaml
docker compose up -d
```

Common commands:
- Logs: `docker compose logs -f`
- Stop: `docker compose down`
- Update: `git pull && docker compose up -d --build`

## Configuration
- Environment selection: `GRVT_ENV=test|prod` (from `.env` or shell)
- Config files are loaded **only** from `config/<env>/...` (there is no root `config.yaml`).
- Thresholds live in `config/<env>/config.yaml`; account secrets/IDs live in `config/<env>/account_*_config.yaml` and `.env`.

## Key logic (how decisions are made)
**Rebalance**
- Fetch trading summary for A and B: `eq`, `mm`, `avail`
- Compute `delta = eqA - eqB`
- If `|delta| <= triggerValue`: no transfer
- Else transfer `needed = |delta| / 2` from the higher-equity account to the lower-equity account, capped by:
  - `max_by_avail = available_balance`
  - `max_by_mm = equity - 2 * maintenance_margin` (keeps a buffer)
  - `transfer = min(needed, max_by_avail, max_by_mm)`

**Funding sweep**
- If funding USDT > `fundingSweepThreshold`, the bot sweeps funds back into the trading subaccount before rebalancing.

**Available-balance alert**
- Alert when `(available_balance / total_equity * 100) < minAvailableBalanceAlertPercentage`.

**Emergency unwind (optional)**
- Margin usage = `maintenance_margin / total_equity * 100`
- Trigger when either account >= `unwind.triggerPct`, stop when both < `unwind.recoveryPct`
- Per iteration, pick hedged instruments (present in both accounts) and place **reduce-only market orders** on both sides
- Order sizing is percentage-based:
  - Compute a stress-based ratio toward recovery
  - Cap it by `unwind.unwindPct` (if configured), then apply the same ratio to each matched position (bounded by min notional and instrument step size)

## Telegram
- Alerts are always outbound.
- Commands (`/start`, `/view`) are processed only from `TELEGRAM_CHAT_ID` (if set), to prevent strangers from interacting with your bot.

## Advanced (Python)
If you prefer running from source:
```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt  # Windows
python rebalance_trading_equity.py
```

## Project structure
- `rebalance_trading_equity.py`: main loop + logging + starts Telegram bot daemon
- `rebalance/services.py`: summaries, rebalance decisioning
- `flow.py`: transfer flow (Trading → Funding → Funding → Trading)
- `unwind/services.py`: emergency unwind (reduce-only market orders)
- `alerts/services.py`: Telegram alerts

## License
MIT (see `LICENSE`).
