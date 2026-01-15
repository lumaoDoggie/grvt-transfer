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
    # Backward-compatible helper (old settings assume prod).
    return _load_yaml_optional(os.path.join("config", "prod", "config.yaml"))


def _env_defaults(env: str) -> dict:
    env = str(env or "prod").lower()
    return _load_yaml_optional(os.path.join("config", env, "config.yaml"))


def _read_settings() -> dict:
    p = _settings_path()
    try:
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8")) or {}
            # Migrate old flat format to per-env structure.
            if "envs" not in raw:
                prod = {
                    "telegram_token": raw.get("telegram_token", ""),
                    "telegram_chat_id": raw.get("telegram_chat_id", ""),
                    "account_a": raw.get("account_a") or {},
                    "account_b": raw.get("account_b") or {},
                    "base_cfg": raw.get("base_cfg") or {},
                }
                return {"selected_env": "prod", "envs": {"prod": prod}}
            return raw
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
    from urllib.error import HTTPError
    import json as _json

    try:
        with urlopen(url, timeout=25) as resp:
            return _json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        try:
            obj = _json.loads(body) if body else {}
        except Exception:
            obj = {"raw": body}
        return {"ok": False, "http_status": int(getattr(e, "code", 0) or 0), "error": str(e), "body": obj}


def _telegram_post_json(url: str, payload: dict) -> dict:
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    import json as _json

    data = _json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=25) as resp:
            return _json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        try:
            obj = _json.loads(body) if body else {}
        except Exception:
            obj = {"raw": body}
        return {"ok": False, "http_status": int(getattr(e, "code", 0) or 0), "error": str(e), "body": obj}


def _validate_telegram(token: str, chat_id: str) -> tuple[bool, str]:
    token = (token or "").strip()
    chat_id = str(chat_id or "").strip()
    if not token or not chat_id:
        return False, "Telegram: 缺少 token 或 chat_id"

    me = _telegram_get_json(f"https://api.telegram.org/bot{token}/getMe")
    if not me.get("ok"):
        return False, (
            "Telegram: getMe 失败。可能原因：token 错误/网络无法访问 api.telegram.org。\n"
            f"detail={me}"
        )

    text = f"grvt-transfer 验证成功 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    res = _telegram_post_json(
        f"https://api.telegram.org/bot{token}/sendMessage",
        {"chat_id": chat_id, "text": text},
    )
    if not res.get("ok"):
        return False, (
            "Telegram: sendMessage 失败。常见原因：chat_id 不对 / 你还没在 Telegram 里点过该机器人并 /start。\n"
            f"detail={res}"
        )
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

    import dataclasses

    # Trading summary (auth + subaccount id correctness). Retry on transient network errors.
    client_t = ClientFactory.trading_client(cfg)
    sub_id = str(cfg.get("trading_account_id"))
    last_exc = None
    for attempt in range(3):
        try:
            res_t = client_t.sub_account_summary_v1(rt.ApiSubAccountSummaryRequest(sub_account_id=sub_id))
            if isinstance(res_t, GrvtError):
                return False, f"{name}: Trading summary 失败: {dataclasses.asdict(res_t)}"
            break
        except Exception as e:
            last_exc = e
            time.sleep(1 + attempt)
    else:
        return False, f"{name}: Trading summary 异常: {last_exc}"

    # Funding summary (auth correctness). Retry on transient network errors.
    client_f = ClientFactory.funding_client(cfg)
    last_exc = None
    for attempt in range(3):
        try:
            res_f = client_f.funding_account_summary_v1(types.EmptyRequest())
            if isinstance(res_f, GrvtError):
                return False, f"{name}: Funding summary 失败: {dataclasses.asdict(res_f)}"
            break
        except Exception as e:
            last_exc = e
            time.sleep(1 + attempt)
    else:
        return False, f"{name}: Funding summary 异常: {last_exc}"

    return True, f"{name}: 验证通过"


@dataclass
class GuiSettings:
    env: str = "prod"
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

        saved = _read_settings() or {}
        selected_env = str(saved.get("selected_env") or "prod").lower()
        envs = dict(saved.get("envs") or {})
        env_blob = dict(envs.get(selected_env) or {})

        defaults = _env_defaults(selected_env) or {}
        base_cfg = dict(defaults)
        try:
            base_cfg.update(dict(env_blob.get("base_cfg") or {}))
        except Exception:
            pass

        self.settings = GuiSettings(
            env=selected_env,
            telegram_token=str(env_blob.get("telegram_token", "")),
            telegram_chat_id=str(env_blob.get("telegram_chat_id", "")),
            account_a=dict(env_blob.get("account_a") or {}),
            account_b=dict(env_blob.get("account_b") or {}),
            base_cfg=base_cfg,
        )

        self._runner: RebalanceRunner | None = None
        self._validated_ok = False
        # maxIterations removed from UI; keep internal default as 0 (= no limit).

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
        frm_tg = ttk.LabelFrame(self.tab_main, text="Telegram（可选）")
        frm_tg.pack(fill="x", padx=5, pady=5)

        frm_env = ttk.LabelFrame(self.tab_main, text="环境")
        frm_env.pack(fill="x", padx=5, pady=5)

        self.v_env_label = StringVar()
        ttk.Label(frm_env, text="运行环境").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.cmb_env = ttk.Combobox(frm_env, textvariable=self.v_env_label, state="readonly", values=["生产", "测试"], width=12)
        self.cmb_env.grid(row=0, column=1, sticky="w", padx=5, pady=5)
        self.cmb_env.bind("<<ComboboxSelected>>", self._on_env_changed)

        self.v_tg_token = StringVar()
        self.v_tg_chat = StringVar()
        ttk.Label(frm_tg, text="机器人 Token").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        ttk.Entry(frm_tg, textvariable=self.v_tg_token, width=60).grid(row=0, column=1, sticky="we", padx=5, pady=5)
        ttk.Label(frm_tg, text="Chat ID").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        ttk.Entry(frm_tg, textvariable=self.v_tg_chat, width=60).grid(row=1, column=1, sticky="we", padx=5, pady=5)
        frm_tg.columnconfigure(1, weight=1)

        frm_a = ttk.LabelFrame(self.tab_main, text="账户A")
        frm_a.pack(fill="x", padx=5, pady=5)
        frm_b = ttk.LabelFrame(self.tab_main, text="账户B")
        frm_b.pack(fill="x", padx=5, pady=5)

        self.acc_fields = [
            ("account_id", "账户ID (account_id)"),
            ("funding_account_address", "资金账户地址 (funding_account_address)"),
            ("fundingAccountKey", "资金账户Key (fundingAccountKey)"),
            ("fundingAccountSecret", "资金账户Secret (fundingAccountSecret)"),
            ("trading_account_id", "交易账户ID (tradingAccountId)"),
            ("tradingAccountKey", "交易账户Key (tradingAccountKey)"),
            ("tradingAccountSecret", "交易账户Secret (tradingAccountSecret)"),
        ]
        self.v_a = {k: StringVar() for k, _ in self.acc_fields}
        self.v_b = {k: StringVar() for k, _ in self.acc_fields}

        self._build_account_form(frm_a, self.v_a)
        self._build_account_form(frm_b, self.v_b)

        frm_btn = ttk.Frame(self.tab_main)
        frm_btn.pack(fill="x", padx=5, pady=8)

        self.btn_validate = ttk.Button(frm_btn, text="验证", command=self.on_validate)
        # Start should be allowed even if the user skips validation.
        self.btn_start = ttk.Button(frm_btn, text="开始", command=self.on_start, state="normal")
        self.btn_stop = ttk.Button(frm_btn, text="停止", command=self.on_stop, state="disabled")
        self.btn_clear = ttk.Button(frm_btn, text="删除配置", command=self.on_clear)
        self.btn_validate.pack(side="left", padx=5)
        self.btn_start.pack(side="left", padx=5)
        self.btn_stop.pack(side="left", padx=5)

        self.lbl_state = ttk.Label(frm_btn, text="状态: 未运行")
        self.lbl_state.pack(side="right", padx=5)
        self.btn_clear.pack(side="right", padx=5)

        frm_log = ttk.LabelFrame(self.tab_main, text="日志 / 验证结果")
        frm_log.pack(fill="both", expand=True, padx=5, pady=5)
        self.txt_log = Text(frm_log, height=14, state="disabled")
        self.txt_log.pack(fill="both", expand=True, padx=5, pady=5)
        self.log = TkLog(self.txt_log)

        # ---- Advanced tab
        frm_cfg = ttk.LabelFrame(self.tab_adv, text="运行参数")
        frm_cfg.pack(fill="x", padx=5, pady=5)

        self.v_adv_note = StringVar(value="")
        ttk.Label(frm_cfg, textvariable=self.v_adv_note).grid(row=0, column=0, columnspan=2, sticky="w", padx=5, pady=2)

        self.v_trigger = StringVar()
        self.v_interval = StringVar()
        self.v_sweep = StringVar()
        self.v_min_avail_pct = StringVar()

        # Track widgets that should be disabled while running.
        self._adv_widgets: list = []

        row = 1
        for label, var in [
            ("再平衡触发阈值 (triggerValue, USDT)", self.v_trigger),
            ("检查间隔 (rebalanceIntervalSec, 秒)", self.v_interval),
            ("资金归集阈值 (fundingSweepThreshold, USDT)", self.v_sweep),
            ("可用余额告警阈值 (minAvailableBalanceAlertPercentage, %)", self.v_min_avail_pct),
        ]:
            ttk.Label(frm_cfg, text=label).grid(row=row, column=0, sticky="w", padx=5, pady=5)
            e = ttk.Entry(frm_cfg, textvariable=var, width=30)
            e.grid(row=row, column=1, sticky="w", padx=5, pady=5)
            self._adv_widgets.append(e)
            row += 1

        frm_unwind = ttk.LabelFrame(self.tab_adv, text="紧急减仓（unwind）")
        frm_unwind.pack(fill="x", padx=5, pady=5)

        self.v_unwind_enabled = BooleanVar(value=True)
        self.v_unwind_dryrun = BooleanVar(value=True)
        self.v_unwind_trigger = StringVar()
        self.v_unwind_recovery = StringVar()
        self.v_unwind_wait = StringVar()
        self.v_unwind_min_notional = StringVar()

        cb_enabled = ttk.Checkbutton(frm_unwind, text="启用", variable=self.v_unwind_enabled)
        cb_dry = ttk.Checkbutton(frm_unwind, text="dryRun（只告警,不实际减仓）", variable=self.v_unwind_dryrun)
        cb_enabled.grid(row=0, column=0, sticky="w", padx=5, pady=5)
        cb_dry.grid(row=0, column=1, sticky="w", padx=5, pady=5)
        self._adv_widgets.extend([cb_enabled, cb_dry])

        row = 1
        for label, var in [
            ("触发阈值 (triggerPct, %)", self.v_unwind_trigger),
            ("恢复阈值 (recoveryPct, %)", self.v_unwind_recovery),
            ("轮间等待 (waitSecondsBetweenIterations, 秒)", self.v_unwind_wait),
            ("最小名义价值 (minPositionNotional, USDT)", self.v_unwind_min_notional),
        ]:
            ttk.Label(frm_unwind, text=label).grid(row=row, column=0, sticky="w", padx=5, pady=5)
            e = ttk.Entry(frm_unwind, textvariable=var, width=30)
            e.grid(row=row, column=1, sticky="w", padx=5, pady=5)
            self._adv_widgets.append(e)
            row += 1

    def _build_account_form(self, parent, vars_map: dict):
        for r, (k, label) in enumerate(self.acc_fields):
            ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", padx=5, pady=3)
            show = "*" if "Secret" in label else ""
            ttk.Entry(parent, textvariable=vars_map[k], width=70, show=show).grid(row=r, column=1, sticky="we", padx=5, pady=3)
        parent.columnconfigure(1, weight=1)

    def _load_into_ui(self):
        self.v_env_label.set("生产" if self.settings.env == "prod" else "测试")
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
        self.v_unwind_wait.set(str(uw.get("waitSecondsBetweenIterations", "")))
        self.v_unwind_min_notional.set(str(uw.get("minPositionNotional", "")))

    def _gather_from_ui(self) -> GuiSettings:
        env = "prod" if (self.v_env_label.get() == "生产") else "test"
        base = dict(self.settings.base_cfg or {})
        base["environment"] = env
        base["triggerValue"] = float(self.v_trigger.get().strip() or 0)
        base["rebalanceIntervalSec"] = int(float(self.v_interval.get().strip() or 15))
        base["fundingSweepThreshold"] = float(self.v_sweep.get().strip() or 0)
        base["minAvailableBalanceAlertPercentage"] = float(self.v_min_avail_pct.get().strip() or 0)

        base["unwind"] = {
            "enabled": bool(self.v_unwind_enabled.get()),
            "dryRun": bool(self.v_unwind_dryrun.get()),
            "triggerPct": float(self.v_unwind_trigger.get().strip() or 0),
            "recoveryPct": float(self.v_unwind_recovery.get().strip() or 0),
            "waitSecondsBetweenIterations": int(float(self.v_unwind_wait.get().strip() or 2)),
            "minPositionNotional": float(self.v_unwind_min_notional.get().strip() or 0),
        }

        def gather_acc(vars_map: dict) -> dict:
            d = {}
            for k, _ in self.acc_fields:
                d[k] = str(vars_map[k].get() or "").strip()
            d["environment"] = env
            return d

        return GuiSettings(
            env=env,
            telegram_token=str(self.v_tg_token.get() or "").strip(),
            telegram_chat_id=str(self.v_tg_chat.get() or "").strip(),
            account_a=gather_acc(self.v_a),
            account_b=gather_acc(self.v_b),
            base_cfg=base,
        )

    def _persist(self, gs: GuiSettings) -> None:
        raw = _read_settings() or {"selected_env": "prod", "envs": {}}
        envs = dict(raw.get("envs") or {})
        envs[str(gs.env or "prod").lower()] = {
            "telegram_token": gs.telegram_token,
            "telegram_chat_id": gs.telegram_chat_id,
            "account_a": gs.account_a or {},
            "account_b": gs.account_b or {},
            "base_cfg": gs.base_cfg or {},
        }
        raw["selected_env"] = str(gs.env or "prod").lower()
        raw["envs"] = envs
        _write_settings(raw)

    def _on_env_changed(self, _evt=None):
        if self._runner and self._runner.running():
            # Don't allow switching env mid-run.
            self.v_env_label.set("生产" if self.settings.env == "prod" else "测试")
            messagebox.showwarning("提示", "运行中无法切换环境，请先停止。")
            return

        gs = self._gather_from_ui()
        # Save current env inputs before switching.
        try:
            self._persist(gs)
        except Exception:
            pass

        new_env = "prod" if (self.v_env_label.get() == "生产") else "test"
        raw = _read_settings() or {"selected_env": new_env, "envs": {}}
        env_blob = dict((raw.get("envs") or {}).get(new_env) or {})

        defaults = _env_defaults(new_env) or {}
        base_cfg = dict(defaults)
        try:
            base_cfg.update(dict(env_blob.get("base_cfg") or {}))
        except Exception:
            pass

        self.settings = GuiSettings(
            env=new_env,
            telegram_token=str(env_blob.get("telegram_token", "")),
            telegram_chat_id=str(env_blob.get("telegram_chat_id", "")),
            account_a=dict(env_blob.get("account_a") or {}),
            account_b=dict(env_blob.get("account_b") or {}),
            base_cfg=base_cfg,
        )
        self._validated_ok = False
        self.btn_start.configure(state="disabled")
        self._load_into_ui()
        self.log.write(f"已切换环境：{'生产' if new_env == 'prod' else '测试'}")

    def _set_running_ui(self, running: bool) -> None:
        self.btn_validate.configure(state=("disabled" if running else "normal"))
        # Start is allowed without validation; validation just reduces surprises.
        self.btn_start.configure(state=("disabled" if running else "normal"))
        self.btn_stop.configure(state=("normal" if running else "disabled"))
        self.lbl_state.configure(text=("状态: 运行中" if running else "状态: 未运行"))
        self._set_adv_enabled(not running, reason=("运行中：参数已锁定，停止后可修改" if running else ""))
        # Prevent switching env while running (also enforced in handler).
        self.cmb_env.configure(state=("disabled" if running else "readonly"))

    def _set_stopping_ui(self) -> None:
        self.btn_validate.configure(state="disabled")
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="disabled")
        self.lbl_state.configure(text="状态: 停止中…")
        self._set_adv_enabled(False, reason="停止中：参数已锁定，等待当前调用结束…")
        self.cmb_env.configure(state="disabled")

    def _set_adv_enabled(self, enabled: bool, reason: str = "") -> None:
        self.v_adv_note.set(reason or "")
        state = "normal" if enabled else "disabled"
        for w in getattr(self, "_adv_widgets", []) or []:
            try:
                w.configure(state=state)
            except Exception:
                pass

    def _poll_stop_complete(self, started_at: float) -> None:
        r = self._runner
        if not r:
            self._set_running_ui(False)
            return
        if not r.running():
            # Finalize/cleanup thread reference promptly.
            try:
                r.stop(timeout_sec=0)
            except Exception:
                pass
            self.log.write("已停止。")
            self._set_running_ui(False)
            return
        # Still running: keep polling without blocking the UI thread.
        if time.time() - started_at > 30:
            self.log.write("仍在停止中（可能在等待当前 API 调用超时/返回）…")
            started_at = time.time()
        self.root.after(300, lambda: self._poll_stop_complete(started_at))

    def on_validate(self):
        if self._runner and self._runner.running():
            messagebox.showwarning("提示", "正在运行中，无法验证。请先停止。")
            return

        gs = self._gather_from_ui()
        self._persist(gs)

        # Export Telegram + env settings to env so the running loop/bot uses them.
        os.environ["TELEGRAM_BOT_TOKEN"] = gs.telegram_token
        os.environ["TELEGRAM_CHAT_ID"] = gs.telegram_chat_id
        os.environ["GRVT_ENV"] = gs.env

        self._validated_ok = False
        self.btn_start.configure(state="disabled")
        self.log.write("开始验证…")

        def work():
            ok_all = True
            # Validate Telegram + two accounts in parallel to reduce total wait time.
            results: dict[str, bool] = {"TG": True, "A": False, "B": False}

            def _vtg():
                token = str(gs.telegram_token or "").strip()
                chat_id = str(gs.telegram_chat_id or "").strip()
                if not token or not chat_id:
                    self.log.write("Telegram: 未填写完整，跳过验证（可选）")
                    results["TG"] = True
                    return
                try:
                    ok, msg = _validate_telegram(token, chat_id)
                    self.log.write(msg)
                    results["TG"] = bool(ok)
                except Exception as e:
                    self.log.write(f"Telegram: 验证异常: {e}")
                    results["TG"] = False

            def _va():
                try:
                    ok, msg = _validate_grvt_account("账户A", gs.account_a or {})
                    self.log.write(msg)
                    results["A"] = bool(ok)
                except Exception as e:
                    self.log.write(f"账户A: 验证异常: {e}")
                    results["A"] = False

            def _vb():
                try:
                    ok, msg = _validate_grvt_account("账户B", gs.account_b or {})
                    self.log.write(msg)
                    results["B"] = bool(ok)
                except Exception as e:
                    self.log.write(f"账户B: 验证异常: {e}")
                    results["B"] = False

            ta = threading.Thread(target=_va, daemon=True)
            tb = threading.Thread(target=_vb, daemon=True)
            ttg = threading.Thread(target=_vtg, daemon=True)
            ttg.start()
            ta.start()
            tb.start()
            ttg.join()
            ta.join()
            tb.join()
            ok_all = ok_all and results["TG"] and results["A"] and results["B"]

            self.root.after(0, lambda: self._on_validate_done(ok_all))

        threading.Thread(target=work, daemon=True).start()

    def _on_validate_done(self, ok_all: bool):
        self._validated_ok = bool(ok_all)
        if ok_all:
            self.log.write("全部验证通过。可以点击【开始】。")
        else:
            self.log.write("验证失败：请修正配置后重试。")
        # Start is allowed regardless of validation outcome (it will ask for confirmation).
        self.btn_start.configure(state="normal")

    def on_start(self):
        if self._runner and self._runner.running():
            return

        gs = self._gather_from_ui()
        self._persist(gs)

        # Basic sanity: account credentials are required to run. Telegram is optional.
        def _missing_required(acc: dict) -> list[str]:
            required = [
                "account_id",
                "funding_account_address",
                "fundingAccountKey",
                "fundingAccountSecret",
                "trading_account_id",
                "tradingAccountKey",
                "tradingAccountSecret",
            ]
            return [k for k in required if not str((acc or {}).get(k, "")).strip()]

        miss_a = _missing_required(gs.account_a or {})
        miss_b = _missing_required(gs.account_b or {})
        if miss_a or miss_b:
            parts = []
            if miss_a:
                parts.append("账户A: " + ", ".join(miss_a))
            if miss_b:
                parts.append("账户B: " + ", ".join(miss_b))
            messagebox.showerror("缺少必填项", "请先填写账户凭证：\n" + "\n".join(parts))
            return

        if not self._validated_ok:
            ok = messagebox.askyesno("未验证", "尚未验证凭证（推荐先点【验证】）。仍然开始运行吗？")
            if not ok:
                return

        os.environ["TELEGRAM_BOT_TOKEN"] = gs.telegram_token
        os.environ["TELEGRAM_CHAT_ID"] = gs.telegram_chat_id
        os.environ["GRVT_ENV"] = gs.env

        repo = InMemoryConfigRepository(gs.env, gs.base_cfg or {}, gs.account_a or {}, gs.account_b or {})
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
        self.log.write("停止中...")
        self._set_stopping_ui()
        try:
            self._runner.request_stop()
        except Exception:
            pass
        self.root.after(300, lambda: self._poll_stop_complete(time.time()))

    def on_clear(self):
        if self._runner and self._runner.running():
            messagebox.showwarning("提示", "运行中无法清空，请先停止。")
            return
        ok = messagebox.askyesno("确认清空", "确定要清空当前界面填写的内容吗？（会同时清空本地保存）")
        if not ok:
            return

        env = "prod" if (self.v_env_label.get() == "生产") else "test"
        defaults = _env_defaults(env) or {}

        # Reset in-memory settings so _load_into_ui() doesn't re-populate old values.
        self.settings = GuiSettings(
            env=env,
            telegram_token="",
            telegram_chat_id="",
            account_a={},
            account_b={},
            base_cfg=dict(defaults),
        )
        self._validated_ok = False
        self._load_into_ui()

        # Also clear persisted settings for BOTH envs to avoid surprises on next launch.
        try:
            prod_defaults = _env_defaults("prod") or {}
            test_defaults = _env_defaults("test") or {}
            blank_prod = {"telegram_token": "", "telegram_chat_id": "", "account_a": {}, "account_b": {}, "base_cfg": dict(prod_defaults)}
            blank_test = {"telegram_token": "", "telegram_chat_id": "", "account_a": {}, "account_b": {}, "base_cfg": dict(test_defaults)}
            _write_settings({"selected_env": env, "envs": {"prod": blank_prod, "test": blank_test}})
        except Exception:
            pass
        self.log.write("已清空并保存。")


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
