import json
import os
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import Tk, StringVar, BooleanVar, Text, ttk, messagebox

import yaml

from eth_account import Account as EthAccount
from pysdk.grvt_raw_base import GrvtError
from pysdk.grvt_raw_sync import types
from pysdk import grvt_raw_types as rt

from repository import ClientFactory
from grvt_transfer.runner import InMemoryConfigRepository, RebalanceRunner


def _settings_path() -> Path:
    base = os.getenv("APPDATA") or str(Path.home())
    return Path(base) / "grvt-transfer" / "settings.json"


def _load_yaml_optional(path: str) -> dict:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
    except Exception:
        pass
    return {}


def _prod_defaults() -> dict:
    # Use repo defaults as initial GUI values.
    return _load_yaml_optional(os.path.join("config", "prod", "config.yaml"))


def _read_settings() -> dict:
    p = _settings_path()
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _write_settings(data: dict) -> None:
    p = _settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _telegram_get_json(url: str) -> dict:
    # Avoid adding requests dependency; stdlib only.
    from urllib.request import urlopen
    import json as _json

    with urlopen(url, timeout=25) as resp:
        return _json.loads(resp.read().decode("utf-8"))


def _telegram_post_json(url: str, payload: dict) -> dict:
    from urllib.request import Request, urlopen
    import json as _json

    data = _json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=25) as resp:
        return _json.loads(resp.read().decode("utf-8"))


def _validate_telegram(token: str, chat_id: str) -> tuple[bool, str]:
    token = (token or "").strip()
    chat_id = str(chat_id or "").strip()
    if not token or not chat_id:
        return False, "Telegram: 缺少 token 或 chat_id"

    me = _telegram_get_json(f"https://api.telegram.org/bot{token}/getMe")
    if not me.get("ok"):
        return False, f"Telegram: getMe 失败: {me}"

    text = f"grvt-transfer 验证成功 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    res = _telegram_post_json(
        f"https://api.telegram.org/bot{token}/sendMessage",
        {"chat_id": chat_id, "text": text},
    )
    if not res.get("ok"):
        return False, f"Telegram: sendMessage 失败: {res}"
    return True, "Telegram: 验证通过（已发送测试消息）"


def _validate_grvt_account(name: str, cfg: dict) -> tuple[bool, str]:
    # Required fields (the API calls below should also fail if these are wrong).
    required = [
        "account_id",
        "funding_account_address",
        "fundingAccountKey",
        "fundingAccountSecret",
        "trading_account_id",
        "tradingAccountKey",
        "tradingAccountSecret",
    ]
    missing = [k for k in required if not str(cfg.get(k, "")).strip()]
    if missing:
        return False, f"{name}: 缺少字段: {', '.join(missing)}"

    # Private key format sanity check (fast, local).
    try:
        EthAccount.from_key(str(cfg.get("fundingAccountSecret")))
        EthAccount.from_key(str(cfg.get("tradingAccountSecret")))
    except Exception as e:
        return False, f"{name}: 私钥格式不合法: {e}"

    # Force prod for GUI.
    os.environ["GRVT_ENV"] = "prod"

    # Trading summary (auth + subaccount id correctness).
    try:
        client_t = ClientFactory.trading_client(cfg)
        sub_id = str(cfg.get("trading_account_id"))
        res_t = client_t.sub_account_summary_v1(rt.ApiSubAccountSummaryRequest(sub_account_id=sub_id))
        if isinstance(res_t, GrvtError):
            return False, f"{name}: Trading summary 失败: {res_t}"
    except Exception as e:
        return False, f"{name}: Trading summary 异常: {e}"

    # Funding summary (auth correctness).
    try:
        client_f = ClientFactory.funding_client(cfg)
        res_f = client_f.funding_account_summary_v1(types.EmptyRequest())
        if isinstance(res_f, GrvtError):
            return False, f"{name}: Funding summary 失败: {res_f}"
    except Exception as e:
        return False, f"{name}: Funding summary 异常: {e}"

    return True, f"{name}: 验证通过"


@dataclass
class GuiSettings:
    telegram_token: str = ""
    telegram_chat_id: str = ""
    account_a: dict | None = None
    account_b: dict | None = None
    base_cfg: dict | None = None


class TkLog:
    def __init__(self, text_widget):
        self._text = text_widget
        self._q: queue.Queue[str] = queue.Queue()
        self._text.after(150, self._drain)

    def write(self, line: str) -> None:
        try:
            self._q.put_nowait(line)
        except Exception:
            pass

    def _drain(self):
        try:
            while True:
                line = self._q.get_nowait()
                self._text.configure(state="normal")
                self._text.insert("end", line + "\n")
                self._text.see("end")
                self._text.configure(state="disabled")
        except queue.Empty:
            pass
        finally:
            self._text.after(150, self._drain)


class App:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("GRVT Rebalance & Transfer Bot")

        os.environ["GRVT_ENV"] = "prod"

        # Defaults from prod config (editable in GUI).
        defaults = _prod_defaults() or {}

        saved = _read_settings()
        self.settings = GuiSettings(
            telegram_token=str(saved.get("telegram_token", "")),
            telegram_chat_id=str(saved.get("telegram_chat_id", "")),
            account_a=dict(saved.get("account_a") or {}),
            account_b=dict(saved.get("account_b") or {}),
            base_cfg=dict(defaults),
        )
        # Overlay saved base_cfg (if present) on top of prod defaults.
        try:
            b = dict(saved.get("base_cfg") or {})
            self.settings.base_cfg.update(b)
        except Exception:
            pass

        self._runner: RebalanceRunner | None = None
        self._validated_ok = False

        self._build_ui()
        self._load_into_ui()

    def _build_ui(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=10, pady=10)

        self.tab_main = ttk.Frame(nb)
        self.tab_adv = ttk.Frame(nb)
        nb.add(self.tab_main, text="配置")
        nb.add(self.tab_adv, text="高级")

        # ---- Main tab
        frm_tg = ttk.LabelFrame(self.tab_main, text="Telegram")
        frm_tg.pack(fill="x", padx=5, pady=5)

        self.v_tg_token = StringVar()
        self.v_tg_chat = StringVar()
        ttk.Label(frm_tg, text="Bot Token").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        ttk.Entry(frm_tg, textvariable=self.v_tg_token, width=60).grid(row=0, column=1, sticky="we", padx=5, pady=5)
        ttk.Label(frm_tg, text="Chat ID").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        ttk.Entry(frm_tg, textvariable=self.v_tg_chat, width=60).grid(row=1, column=1, sticky="we", padx=5, pady=5)
        frm_tg.columnconfigure(1, weight=1)

        frm_a = ttk.LabelFrame(self.tab_main, text="账户A")
        frm_a.pack(fill="x", padx=5, pady=5)
        frm_b = ttk.LabelFrame(self.tab_main, text="账户B")
        frm_b.pack(fill="x", padx=5, pady=5)

        self.acc_fields = [
            ("account_id", "Account ID"),
            ("funding_account_address", "Funding Address"),
            ("fundingAccountKey", "Funding Key"),
            ("fundingAccountSecret", "Funding Secret"),
            ("trading_account_id", "Trading SubAccount ID"),
            ("tradingAccountKey", "Trading Key"),
            ("tradingAccountSecret", "Trading Secret"),
        ]
        self.v_a = {k: StringVar() for k, _ in self.acc_fields}
        self.v_b = {k: StringVar() for k, _ in self.acc_fields}

        self._build_account_form(frm_a, self.v_a)
        self._build_account_form(frm_b, self.v_b)

        frm_btn = ttk.Frame(self.tab_main)
        frm_btn.pack(fill="x", padx=5, pady=8)

        self.btn_validate = ttk.Button(frm_btn, text="验证", command=self.on_validate)
        self.btn_start = ttk.Button(frm_btn, text="开始", command=self.on_start, state="disabled")
        self.btn_stop = ttk.Button(frm_btn, text="停止", command=self.on_stop, state="disabled")
        self.btn_validate.pack(side="left", padx=5)
        self.btn_start.pack(side="left", padx=5)
        self.btn_stop.pack(side="left", padx=5)

        self.lbl_state = ttk.Label(frm_btn, text="状态: 未运行")
        self.lbl_state.pack(side="right", padx=5)

        frm_log = ttk.LabelFrame(self.tab_main, text="日志 / 验证结果")
        frm_log.pack(fill="both", expand=True, padx=5, pady=5)
        self.txt_log = Text(frm_log, height=14, state="disabled")
        self.txt_log.pack(fill="both", expand=True, padx=5, pady=5)
        self.log = TkLog(self.txt_log)

        # ---- Advanced tab
        frm_cfg = ttk.LabelFrame(self.tab_adv, text="运行参数")
        frm_cfg.pack(fill="x", padx=5, pady=5)

        self.v_trigger = StringVar()
        self.v_interval = StringVar()
        self.v_sweep = StringVar()
        self.v_min_avail_pct = StringVar()

        row = 0
        for label, var in [
            ("triggerValue (USDT)", self.v_trigger),
            ("rebalanceIntervalSec (秒)", self.v_interval),
            ("fundingSweepThreshold (USDT)", self.v_sweep),
            ("minAvailableBalanceAlertPercentage (%)", self.v_min_avail_pct),
        ]:
            ttk.Label(frm_cfg, text=label).grid(row=row, column=0, sticky="w", padx=5, pady=5)
            ttk.Entry(frm_cfg, textvariable=var, width=30).grid(row=row, column=1, sticky="w", padx=5, pady=5)
            row += 1

        frm_unwind = ttk.LabelFrame(self.tab_adv, text="紧急减仓（unwind）")
        frm_unwind.pack(fill="x", padx=5, pady=5)

        self.v_unwind_enabled = BooleanVar(value=True)
        self.v_unwind_dryrun = BooleanVar(value=True)
        self.v_unwind_trigger = StringVar()
        self.v_unwind_recovery = StringVar()
        self.v_unwind_max_iter = StringVar()
        self.v_unwind_wait = StringVar()
        self.v_unwind_min_notional = StringVar()

        ttk.Checkbutton(frm_unwind, text="启用", variable=self.v_unwind_enabled).grid(row=0, column=0, sticky="w", padx=5, pady=5)
        ttk.Checkbutton(frm_unwind, text="dryRun（只演练不下单）", variable=self.v_unwind_dryrun).grid(row=0, column=1, sticky="w", padx=5, pady=5)

        row = 1
        for label, var in [
            ("triggerPct (%)", self.v_unwind_trigger),
            ("recoveryPct (%)", self.v_unwind_recovery),
            ("maxIterations", self.v_unwind_max_iter),
            ("waitSecondsBetweenIterations (秒)", self.v_unwind_wait),
            ("minPositionNotional (USDT)", self.v_unwind_min_notional),
        ]:
            ttk.Label(frm_unwind, text=label).grid(row=row, column=0, sticky="w", padx=5, pady=5)
            ttk.Entry(frm_unwind, textvariable=var, width=30).grid(row=row, column=1, sticky="w", padx=5, pady=5)
            row += 1

    def _build_account_form(self, parent, vars_map: dict):
        for r, (k, label) in enumerate(self.acc_fields):
            ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", padx=5, pady=3)
            show = "*" if "Secret" in label else ""
            ttk.Entry(parent, textvariable=vars_map[k], width=70, show=show).grid(row=r, column=1, sticky="we", padx=5, pady=3)
        parent.columnconfigure(1, weight=1)

    def _load_into_ui(self):
        self.v_tg_token.set(self.settings.telegram_token)
        self.v_tg_chat.set(self.settings.telegram_chat_id)

        for k, _ in self.acc_fields:
            self.v_a[k].set(str((self.settings.account_a or {}).get(k, "")))
            self.v_b[k].set(str((self.settings.account_b or {}).get(k, "")))

        b = self.settings.base_cfg or {}
        self.v_trigger.set(str(b.get("triggerValue", "")))
        self.v_interval.set(str(b.get("rebalanceIntervalSec", "")))
        self.v_sweep.set(str(b.get("fundingSweepThreshold", "")))
        self.v_min_avail_pct.set(str(b.get("minAvailableBalanceAlertPercentage", "")))

        uw = (b.get("unwind") or {}) if isinstance(b, dict) else {}
        self.v_unwind_enabled.set(bool(uw.get("enabled", True)))
        self.v_unwind_dryrun.set(bool(uw.get("dryRun", True)))
        self.v_unwind_trigger.set(str(uw.get("triggerPct", "")))
        self.v_unwind_recovery.set(str(uw.get("recoveryPct", "")))
        self.v_unwind_max_iter.set(str(uw.get("maxIterations", "")))
        self.v_unwind_wait.set(str(uw.get("waitSecondsBetweenIterations", "")))
        self.v_unwind_min_notional.set(str(uw.get("minPositionNotional", "")))

    def _gather_from_ui(self) -> GuiSettings:
        base = dict(self.settings.base_cfg or {})
        base["environment"] = "prod"
        base["triggerValue"] = float(self.v_trigger.get().strip() or 0)
        base["rebalanceIntervalSec"] = int(float(self.v_interval.get().strip() or 15))
        base["fundingSweepThreshold"] = float(self.v_sweep.get().strip() or 0)
        base["minAvailableBalanceAlertPercentage"] = float(self.v_min_avail_pct.get().strip() or 0)

        base["unwind"] = {
            "enabled": bool(self.v_unwind_enabled.get()),
            "dryRun": bool(self.v_unwind_dryrun.get()),
            "triggerPct": float(self.v_unwind_trigger.get().strip() or 0),
            "recoveryPct": float(self.v_unwind_recovery.get().strip() or 0),
            "maxIterations": int(float(self.v_unwind_max_iter.get().strip() or 0)),
            "waitSecondsBetweenIterations": int(float(self.v_unwind_wait.get().strip() or 0)),
            "minPositionNotional": float(self.v_unwind_min_notional.get().strip() or 0),
        }

        def gather_acc(vars_map: dict) -> dict:
            d = {}
            for k, _ in self.acc_fields:
                d[k] = str(vars_map[k].get() or "").strip()
            d["environment"] = "prod"
            return d

        return GuiSettings(
            telegram_token=str(self.v_tg_token.get() or "").strip(),
            telegram_chat_id=str(self.v_tg_chat.get() or "").strip(),
            account_a=gather_acc(self.v_a),
            account_b=gather_acc(self.v_b),
            base_cfg=base,
        )

    def _persist(self, gs: GuiSettings) -> None:
        _write_settings(
            {
                "telegram_token": gs.telegram_token,
                "telegram_chat_id": gs.telegram_chat_id,
                "account_a": gs.account_a or {},
                "account_b": gs.account_b or {},
                "base_cfg": gs.base_cfg or {},
            }
        )

    def _set_running_ui(self, running: bool) -> None:
        self.btn_validate.configure(state=("disabled" if running else "normal"))
        self.btn_start.configure(state=("disabled" if running else ("normal" if self._validated_ok else "disabled")))
        self.btn_stop.configure(state=("normal" if running else "disabled"))
        self.lbl_state.configure(text=("状态: 运行中" if running else "状态: 未运行"))

    def on_validate(self):
        if self._runner and self._runner.running():
            messagebox.showwarning("提示", "正在运行中，无法验证。请先停止。")
            return

        gs = self._gather_from_ui()
        self._persist(gs)

        # Export Telegram settings to env so the running loop/bot uses them.
        os.environ["TELEGRAM_BOT_TOKEN"] = gs.telegram_token
        os.environ["TELEGRAM_CHAT_ID"] = gs.telegram_chat_id
        os.environ["GRVT_ENV"] = "prod"

        self._validated_ok = False
        self.btn_start.configure(state="disabled")
        self.log.write("开始验证…")

        def work():
            ok_all = True
            try:
                ok, msg = _validate_telegram(gs.telegram_token, gs.telegram_chat_id)
                self.log.write(msg)
                ok_all = ok_all and ok
            except Exception as e:
                self.log.write(f"Telegram: 验证异常: {e}")
                ok_all = False

            try:
                ok, msg = _validate_grvt_account("账户A", gs.account_a or {})
                self.log.write(msg)
                ok_all = ok_all and ok
            except Exception as e:
                self.log.write(f"账户A: 验证异常: {e}")
                ok_all = False

            try:
                ok, msg = _validate_grvt_account("账户B", gs.account_b or {})
                self.log.write(msg)
                ok_all = ok_all and ok
            except Exception as e:
                self.log.write(f"账户B: 验证异常: {e}")
                ok_all = False

            self.root.after(0, lambda: self._on_validate_done(ok_all))

        threading.Thread(target=work, daemon=True).start()

    def _on_validate_done(self, ok_all: bool):
        self._validated_ok = bool(ok_all)
        if ok_all:
            self.log.write("全部验证通过。可以点击【开始】。")
            self.btn_start.configure(state="normal")
        else:
            self.log.write("验证失败：请修正配置后重试。")
            self.btn_start.configure(state="disabled")

    def on_start(self):
        if not self._validated_ok:
            messagebox.showwarning("提示", "请先点击【验证】并确保全部通过。")
            return
        if self._runner and self._runner.running():
            return

        gs = self._gather_from_ui()
        self._persist(gs)

        os.environ["TELEGRAM_BOT_TOKEN"] = gs.telegram_token
        os.environ["TELEGRAM_CHAT_ID"] = gs.telegram_chat_id
        os.environ["GRVT_ENV"] = "prod"

        repo = InMemoryConfigRepository(gs.base_cfg or {}, gs.account_a or {}, gs.account_b or {})
        self._runner = RebalanceRunner(repo)
        started = self._runner.start()
        if started:
            self.log.write("已开始运行。")
            self._set_running_ui(True)
        else:
            self.log.write("开始失败：已经在运行中。")

    def on_stop(self):
        if not self._runner:
            return
        self.log.write("正在停止…")
        self._runner.stop()
        self.log.write("已停止。")
        self._set_running_ui(False)


def main():
    root = Tk()
    # Use a modern theme on Windows if available.
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.on_stop(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()
