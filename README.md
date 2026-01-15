# GRVT 双账户自动平衡与紧急减仓机器人

Language: 中文 | English: `README.en.md`

这个项目会**同时监控两个 GRVT 交易子账户（A/B）**，并自动完成：
- **资金再平衡（Transfer/Rebalance）**：当 A/B 两个账户的余额差额超过阈值时，自动划转 USDT，让两边更接近
划转路径:  账户A 交易账户(trading账户) --> 账户A 资金账户(funding账户) --> 账户B 资金账户--> 账户B交易账户

- **紧急减仓（Unwind，可选）**：当保证金使用率过高时，按配置自动下 `reduce-only` 市价单做双边减仓，尽量把风险压回安全区
- **Telegram 告警 + 状态查询**：支持告警推送，并提供 `/start`、`/view` 查询运行状态

本脚本分为生产模式, 和 测试模式. (grvt有一个测试站, 里面是单独的账户系统,用于测试.)
你可以先开小仓位, 直接接入生产模式.
或者先接入测试模式, 再接入生产模式. (比如说你已经有比较大的仓位了)

GRVT 站点：
- 生产（Prod）：`https://grvt.io`

---

## 重要安全提示（务必先看）
- 本项目**可能会真实下单**并**真实划转资金**。运行前务必确认你清楚它会做什么，以及每个阈值的含义。
- 跑在生产环境的时候, 建议先用迷你仓位, 测试资金划转和紧急关仓功能
- 在你完全确认逻辑之前，建议保持 `unwind.dryRun: true`（只告警/记录，不真正减仓）。
- 建议设置 `TELEGRAM_CHAT_ID`：意思就是Telegram机器人只能和你互动 (免得别人来获取你的余额变动通知)

---

## Windows 一键运行（推荐：不需要安装 Python）
### 如果在你自己机器跑也OK, 但是务必保证网络稳定. (grvt api调用不用翻墙, tg通知需要)

### Windows GUI（推荐：不用进文件夹改配置）
GUI 提供：`验证`（Telegram + 两个账户）、`开始/停止` 一键运行，并可在界面里选择 `生产/测试` 环境。

- 运行（源码方式）：`python -m grvt_transfer gui` 或 `grvt-transfer gui`
- 配置保存位置：`%APPDATA%\\grvt-transfer\\settings.json`（包含密钥，请自行注意电脑安全）
- 使用流程：填入凭证 → 点击 `验证`（会给你的 Telegram 发一条测试消息）→ 点击 `开始`

命令行运行依然保留：`grvt-transfer run`

1) 从 GitHub Releases 下载 `grvt-transfer-windows.zip` 并解压。
2) 文件夹里复制 `.env.example`,改名为 `.env`，然后编辑 `.env`：
   - `GRVT_ENV=prod` 
   - `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`
   - 两个账户的 API Key / Secret 等（按模板填写）
3) 根据你的需求修改阈值：`config/prod/config.yaml` (生产环境)。
4) 双击 `scripts/windows/Start.bat` 启动。

停止：双击 `scripts/windows/Stop.bat`。

获取 Telegram 的`TELEGRAM_CHAT_ID`：给 在tg上给`@userinfobot` 这个机器人, 发消息即可看到,回复里的那串数字就是。

---

## Linux VPS 部署（推荐：Docker，简单且一致）
前置：VPS 已安装 Docker Engine + Docker Compose 插件。

```bash
git clone <你的仓库地址>
cd grvt-transfer
cp .env.example .env
# 编辑 .env 和 config/<env>/*.yaml
docker compose up -d
```

常用命令：
- 查看日志：`docker compose logs -f`
- 停止服务：`docker compose down`
- 更新版本：`git pull && docker compose up -d --build`

---

## 配置说明（避免歧义）
- 运行环境由 `GRVT_ENV=test|prod` 决定（来自 `.env` 或 shell）
- 配置文件**只会读取** `config/<env>/...`（仓库根目录不存在 `config.yaml`）
- 告警阈值配置在：`config/<env>/config.yaml`
- 账户配置相关在：`config/<env>/account_1_config.yaml`、`config/<env>/account_2_config.yaml` 和 `.env`

---

## 核心逻辑（项目到底怎么做决策）
### 1) 再平衡（Transfer/Rebalance）
每轮循环(默认15秒一次)会读取两个账户的信息, ：
- `total_equity`（余额/权益，按 GRVT API 返回字段使用）
- `maintenance_margin`（维持保证金）
- `available_balance`（可用余额）

再平衡规则（核心思路：把 A/B 拉回“接近一半一半”）：
1) 计算差额：`delta = eqA - eqB`
2) 若 `|delta| <= triggerValue`：不触发划转
3) 否则目标划转值：`needed = |delta| / 2`（把差额对半补齐）
4) 从余额更高的一侧划转到另一侧，并做安全上限：
   - `max_by_avail = available_balance`（可用余额不够就不能转）
   - `max_by_mm = equity - 2 * maintenance_margin`（预留保证金缓冲，避免越转越危险）
   - 最终：`transfer = min(needed, max_by_avail, max_by_mm)`

### 2) Funding 归集（Sweep）
再平衡前会先检查 Funding 账户，如果 Funding 的 USDT 大于 `fundingSweepThreshold`，会自动把资金归集到 Trading 子账户，避免“钱在 Funding 里导致 Trading 可用不足”。

### 3) 可用余额不足告警（Available Balance Alert）
当 `(available_balance / total_equity * 100) < minAvailableBalanceAlertPercentage` 时发送 Telegram 告警。

说明：可用余额不足的情况，一般出现在一个账户大量未实现盈利的时候。这时候你需要手动关一些仓位，释放盈利。

### 4) 紧急减仓（Unwind，可选）
保证金使用率定义：
- `margin_usage_pct = maintenance_margin / total_equity * 100`
- 保证金使用率达到100%就会爆仓. 所以需要一个自动减仓功能,防止爆仓. 默认设置是任一账户达到,60%开始自动减仓, 两个账户都达到 40% 就停止减仓. 

减仓方式（安全优先、尽量保持对冲结构）：
- 只处理“**两边都有仓位**”的币种。若某个标的只在一边有仓位，会被记录并告警。
- 对每个匹配标的，会按两边**较小的仓位**作为基准做同步减仓：`base_size = min(|sizeA|, |sizeB|)`
- 每一轮只按比例减一部分仓位 (防止市价单一次下去,滑点太高了),减仓幅度会动态计算,大概是30秒左右会减仓完毕. 
---

## Telegram 说明
- 告警：只要配置了 `TELEGRAM_BOT_TOKEN` 就会推送（与 `chat_id` 限制无冲突）
- 命令：若设置了 `TELEGRAM_CHAT_ID`，则 `/start`、`/view` 只处理来自该 `chat_id` 的消息，其他人会被忽略

---

## 目录结构（快速定位）
- `rebalance_trading_equity.py`：主循环入口 + 日志 + 启动 Telegram bot
- `rebalance/services.py`：账户摘要、再平衡决策、阈值检查
- `flow.py`：转账流程（Trading → Funding → Funding → Trading）
- `unwind/services.py`：紧急减仓逻辑（reduce-only 市价单）
- `alerts/services.py`：Telegram 告警封装

---

## License
MIT（见 `LICENSE`）
