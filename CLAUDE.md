# GRVT Rebalance & Transfer Bot

- observe guidance in global Claude.md in C:\Users\hecto\.claude  


## Deployment Info
- **Tokyo VPS**: `ubuntu@52.197.20.37:8848`
- **SSH Key**: `C:\Users\hecto\Documents\ssh\aws-tokyo.pem`
- **Remote Directory**: `~/grvt-transfer`

---

## Project Purpose
Automated equity rebalancing and emergency position unwinding system for GRVT exchange accounts. Monitors two trading subaccounts, transfers funds to maintain balanced equity, and automatically unwinds positions when margin ratios fall below safe thresholds. Includes Telegram bot integration for alerts and status monitoring.

---

## Project Structure

```
grvt-transfer/
├── rebalance_trading_equity.py   # Main entry point - runs rebalance loop
├── log-config.yaml               # Python logging configuration
├── requirements.txt              # Dependencies: grvt-pysdk, PyYAML, eth-account
│
├── config/                       # Environment-specific configs
│   ├── prod/
│   │   ├── config.yaml           # Prod settings (triggers, bot token)
│   │   ├── account_1_config.yaml # Prod account A credentials
│   │   └── account_2_config.yaml # Prod account B credentials
│   └── test/
│       ├── config.yaml           # Test settings (lower thresholds, dryRun)
│       ├── account_1_config.yaml # Testnet account A credentials
│       └── account_2_config.yaml # Testnet account B credentials
│
├── repository.py                 # ConfigRepository & ClientFactory classes
├── flow.py                       # TransferFlow & BalanceSweeper logic
├── utils.py                      # TimeUtil, FundingUtil, TxUtil helpers
│
├── rebalance/
│   ├── __init__.py
│   └── services.py               # SummaryService, TransferService, RebalanceService
│
├── unwind/
│   ├── __init__.py
│   └── services.py               # UnwindService - emergency position unwinding
│
├── alerts/
│   ├── __init__.py
│   └── services.py               # AlertService - dispatches to Telegram
│
├── bot/
│   ├── __init__.py
│   ├── config.yaml               # Bot-specific config (can override main)
│   ├── run_bot.py                # Standalone bot runner (for testing)
│   └── telegram_bot.py           # Bot logic: polling, commands, send_message
│
└── logs/                         # Log files (rebalance.log, errors, noop events)
```

---

## Environment Profiles (Test/Prod)

### Run in Production (default)
```bash
python rebalance_trading_equity.py
# Or explicitly:
GRVT_ENV=prod python rebalance_trading_equity.py
```

### Run in Test Mode
```bash
GRVT_ENV=test python rebalance_trading_equity.py
```

### On Windows
```powershell
# Prod (default)
python rebalance_trading_equity.py

# Test
$env:GRVT_ENV="test"; python rebalance_trading_equity.py
```

---

## Architecture Overview

### Core Flow
1. **Main Loop** (`rebalance_trading_equity.py`):
   - Starts Telegram bot daemon
   - Every `rebalanceIntervalSec` seconds, runs `RebalanceService.rebalance_once()`

2. **RebalanceService** (`rebalance/services.py`):
   - Sweeps any idle funding balance → trading subaccount
   - Fetches trading summary (equity, margin, available) for both accounts
   - Calculates delta; if > `triggerValue`, executes transfer
   - Checks for emergency unwind conditions
   - Has retry logic for zero-equity detection (3s delay before alerting)
   - Logs events, sends Telegram alerts

3. **UnwindService** (`unwind/services.py`):
   - Monitors margin ratio: `equity / maintenance_margin`
   - Triggers when ratio < `triggerMultiplier` (e.g., < 2.0×)
   - Unwinds positions by placing reduce-only market orders
   - Uses **custom EIP-712 signing** (not SDK's sign_order) for market orders
   - Rounds order sizes to `tick_size` from instrument metadata
   - Stops when ratio > `recoveryMultiplier` (e.g., > 2.5×)

4. **TransferFlow** (`flow.py`):
   - 3-step transfer: Trading→Funding (source) → Funding→Funding (cross-account) → Funding→Trading (dest)
   - Uses signed transfer requests via GRVT SDK

5. **AlertService** (`alerts/services.py`):
   - Dispatches rebalance events (batched: every 5th event)
   - Unwind alerts: triggered, completed (with token breakdown), recovery
   - Order failure alerts
   - Availability alerts when balance % drops below threshold

6. **Telegram Bot** (`bot/telegram_bot.py`):
   - Long-polling for updates
   - Commands: `/start`, `/view` (shows last noop log line)
   - Lock file prevents duplicate instances

---

## Emergency Unwind Feature

### Configuration (`config.yaml`)
```yaml
unwind:
  enabled: true
  dryRun: true                        # Set false for live orders
  triggerMultiplier: 2.0              # Trigger when equity < 2× maintenance margin
  recoveryMultiplier: 2.5             # Stop when equity > 2.5× maintenance margin
  unwindPct: 10.0                     # Reduce 10% of position per iteration
  maxIterations: 5                    # Max unwind cycles
  waitSecondsBetweenIterations: 5
  minPositionNotional: 100            # Skip positions < $100
```

### How It Works
1. **Trigger**: `equity / maintenance_margin < triggerMultiplier`
2. **Action**: Places reduce-only market orders to close 10% of each position
3. **Recovery**: Stops when ratio > `recoveryMultiplier`

### Key Implementation Details (unwind/services.py)
- **Custom EIP-712 signing**: SDK's `sign_order` doesn't support market orders properly, so we use direct `encode_typed_data` signing
- **Order size rounding**: Uses `tick_size` from instrument metadata (e.g., 0.01 for ETH)
- **Chain ID**: Testnet = 326, Production = 325
- **Client Order ID**: Must be numeric in range [2^63, 2^64-1]
- **Cookie auth**: Uses SDK client's `_cookie.gravity` for REST API calls

### Alert Format
```
🚨 UNWIND TRIGGERED
━━━━━━━━━━━━━━━━━━
⚠️ Account A: 1.85× margin
✅ Account B: 9.88× margin
━━━━━━━━━━━━━━━━━━
Trigger at: <2.0× margin

✅ UNWIND COMPLETED
━━━━━━━━━━━━━━━━━━
Orders: 10✓ 0✗
A: ETH 14.68 ($466,399)
B: ETH 15.50 ($496,038)
━━━━━━━━━━━━━━━━━━
A: 13.14× | B: 13.07×
```

---

## Configuration Reference

### `config.yaml`
```yaml
triggerValue: 2000              # Min delta to trigger rebalance (USDT)
rebalanceIntervalSec: 15        # Check interval
rebalanceOnce: false            # If true, run once and exit
rebalanceThrottleMs: 1500       # Delay between API calls
fundingSweepThreshold: 0.1      # Min balance to sweep to trading
minAvailableBalanceAlertPercentage: 25  # Alert if available < X%
bot:
  token: "<telegram_bot_token>"
  chat_id: "<telegram_chat_id>"
unwind:
  enabled: true
  dryRun: true
  triggerMultiplier: 2.0
  recoveryMultiplier: 2.5
  unwindPct: 10.0
  maxIterations: 5
  waitSecondsBetweenIterations: 5
  minPositionNotional: 100
```

### `account_X_config.yaml`
```yaml
account_id: "0x..."             # Main account address
funding_account_address: "0x..."
trading_account_id: "..."       # Subaccount ID
fundingAccountKey: "..."        # API key
fundingAccountSecret: "0x..."   # Private key for funding
tradingAcccountKey: "..."       # API key for trading (note: typo in key name)
tradingAccountSecret: "0x..."   # Private key for trading
chain_id: 325                   # 325=prod, 326=testnet
currency: "USDT"
```

---

## Key Classes

| Class | File | Purpose |
|-------|------|---------|
| `ConfigRepository` | `repository.py` | Loads YAML configs based on GRVT_ENV |
| `ClientFactory` | `repository.py` | Creates GRVT API clients |
| `SummaryService` | `rebalance/services.py` | Fetches account summaries |
| `TransferService` | `rebalance/services.py` | Builds & executes signed transfers |
| `RebalanceService` | `rebalance/services.py` | Main rebalance logic + unwind check |
| `PositionService` | `unwind/services.py` | Fetches and prioritizes positions |
| `UnwindService` | `unwind/services.py` | Emergency position unwinding |
| `TransferFlow` | `flow.py` | 3-step transfer orchestration |
| `BalanceSweeper` | `flow.py` | Sweeps funding → trading |
| `AlertService` | `alerts/services.py` | Telegram alert dispatch |

---

## Deployment Commands

### SSH into VPS
```bash
ssh -i "C:\Users\hecto\Documents\ssh\aws-tokyo.pem" -p 8848 ubuntu@52.197.20.37
```

### Start the bot
```bash
cd ~/grvt-transfer
rm -f bot/.botlock  # Clear stale lock if needed
nohup venv/bin/python3 rebalance_trading_equity.py > rebalance.log 2>&1 &
```

### Check status
```bash
ps aux | grep python
tail -f ~/grvt-transfer/rebalance.log
```

### Stop the bot
```bash
pkill -f rebalance_trading_equity.py
```

### Deploy updates from local
```powershell
tar -czf deploy.tar.gz --exclude=.git --exclude=__pycache__ --exclude=.vscode .
scp -i "C:\Users\hecto\Documents\ssh\aws-tokyo.pem" -P 8848 deploy.tar.gz ubuntu@52.197.20.37:~/
ssh -i "C:\Users\hecto\Documents\ssh\aws-tokyo.pem" -p 8848 ubuntu@52.197.20.37 "cd grvt-transfer && tar -xzf ~/deploy.tar.gz && rm ~/deploy.tar.gz"
```

---

## Dependencies
- `grvt-pysdk==0.2.1` - GRVT exchange SDK
- `PyYAML>=6.0` - Config parsing
- `eth-account>=0.13.0` - Ethereum signing (also used for EIP-712 order signing)
