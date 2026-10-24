#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每天自动领取 AI 编程工具的免费积分：Qoder 系 + Trae CN + MiniMax Code。

Qoder 不打开桌面端也能领：让常驻程序 QoderWake CN 的本机服务代领。
Trae 读本地登录态换 token 直连接口。
MiniMax Code 读本地 OAuth 凭据；token 快过期时续期，先写回再领。
凭证只在内存中使用，绝不打印、绝不进 git。
"""
import base64
import fcntl
import hashlib
import html as html_lib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "领积分.log"
STATUS_FILE = BASE_DIR / "最新状态.txt"
STATUS_JSON = BASE_DIR / "今日状态.json"
LEDGER_FILE = BASE_DIR / "领积分台账.jsonl"
INV_FILE = BASE_DIR / "积分盘查.json"
WORDS = {"claimed": "已领到", "already": "已领过", "none": "没有可领", "error": "失败"}
LEDGER_LINES = []          # 本次运行攒下的台账行，main 结束时一次性落盘
INVENTORY = []             # 积分盘查：每家一条，main 结束时写 积分盘查.json
QUIET = False              # --balance 单家刷新时不写日志
LOCK_FILE = BASE_DIR / ".run.lock"
_lock_handle = None


def acquire_run_lock(wait_seconds=0):
    """同一时间只允许一轮采集：并发跑会抢 QoderWake 端口，后一轮会把 Qoder 记成失败。"""
    global _lock_handle
    deadline = time.time() + wait_seconds
    while True:
        try:
            handle = open(LOCK_FILE, "w")
        except OSError:
            return True          # 锁文件都建不了，别把主流程挡死
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _lock_handle = handle
            return True
        except OSError:
            handle.close()
            if time.time() >= deadline:
                return False
            time.sleep(0.5)
HOME = Path.home()
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PROXIED = urllib.request.build_opener()   # 走系统代理（macOS 会从网络设置里读）

# 已经领过、或本来就没资格领的状态：跳过，不重复请求
QODER_DONE = {"CLAIMED", "ACHIEVEMENT_NOT_COMPLETED", "EXPIRED", "NOT_ELIGIBLE",
              "UNAVAILABLE", "INELIGIBLE", "ENDED", "FINISHED"}


def _cn_date(text):
    """2026-11-15T… 或 10-26 09:00 → 11月15日 / 10月26日 09:00。"""
    m = re.match(r"(?:(\d{4})-)?(\d{1,2})-(\d{1,2})(?:[T ](\d{2}:\d{2}))?", text or "")
    if not m:
        return text or ""
    out = f"{int(m.group(2))}月{int(m.group(3))}日"
    if m.group(4):
        out += f" {m.group(4)}"
    return out


def log(msg):
    if QUIET:
        return
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _write_atomic(path, text):
    """先写临时文件再替换：定时任务和手动运行可能同时写，别让菜单栏读到半截 JSON。"""
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _num_or_none(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def inventory_add(key, name, app, unit="", total=None, remaining=None,
                  purchased=None, gifted=None, used=None, claimed=None,
                  quota_known=True, expires="", note=""):
    INVENTORY.append({"key": key, "name": name, "app": app, "unit": unit,
                      "total": _num_or_none(total), "remaining": _num_or_none(remaining),
                      "purchased": _num_or_none(purchased), "gifted": _num_or_none(gifted),
                      "used": _num_or_none(used), "claimed": _num_or_none(claimed),
                      "quota_known": quota_known, "expires": expires, "note": note})


def http(url, method="GET", headers=None, body=None, timeout=25):
    """返回 (状态码, 响应头, 解析后的 body)。强制直连，不走系统代理。"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with NO_PROXY.open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        code = resp.status
    try:
        return code, hdrs, json.loads(raw)
    except ValueError:
        return code, hdrs, raw


# ---------------------------------------------------------------- Qoder 系
WAKE_DIR = HOME / ".qoderwake-cn"
WAKE_DEFAULT_PORT = 19830


def wake_port():
    info = WAKE_DIR / "run" / "daemon.info.json"
    if info.exists():
        try:
            return int(json.loads(info.read_text())["port"])
        except Exception:
            pass
    pf = WAKE_DIR / "run" / "daemon.port"
    if pf.exists():
        try:
            return int(pf.read_text().strip())
        except ValueError:
            pass
    return WAKE_DEFAULT_PORT


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


STARTED_PIDS = {"wake": [], "qoder": []}   # 只记我们自己拉起的进程号


def pids_matching(pattern):
    out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.split()
    return [int(x) for x in out]


def alive_pids(pids):
    if not pids:
        return []
    out = subprocess.run(["ps", "-o", "pid=", "-p", ",".join(map(str, pids))],
                         capture_output=True, text=True).stdout.split()
    return [int(x) for x in out]


def quit_app(pids, label):
    """先 SIGTERM 让它自己收尾，10 秒没退就 SIGKILL。
    只动我们自己拉起的进程号，使用者自己在用的实例不在名单里。
    不用 AppleScript quit：对无界面 App 会卡到 AppleEvent 超时（实测 120 秒）。"""
    for pid in alive_pids(pids):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for _ in range(5):
        time.sleep(2)
        if not alive_pids(pids):
            log(f"{label} 已退出")
            return
    hard = alive_pids(pids)
    for pid in hard:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    log(f"{label} 没响应 SIGTERM，已强制结束进程 {hard}")


WAKE_BIN = WAKE_DIR / "qoderwake-cn"


def ensure_wake():
    """确保 QoderWake 本机服务可用，返回 (端口, 是否本次任务启动)。

    直接起它背后的无头守护进程，不开 QoderWake CN.app——那个菜单栏外壳启动时
    会把控制台页在默认浏览器里打开（维护者明确要求不许看到浏览器）。
    """
    port = wake_port()
    if port_open(port):
        return port, False
    if not WAKE_BIN.exists():
        raise RuntimeError("没找到 ~/.qoderwake-cn/qoderwake-cn，无法代领 Qoder 积分")
    proc = subprocess.Popen([str(WAKE_BIN), "__daemon", "--host", "127.0.0.1", "--port", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    for _ in range(20):
        time.sleep(1)
        if port_open(port):
            STARTED_PIDS["wake"] = [proc.pid]
            return port, True
        if proc.poll() is not None:
            break
    raise RuntimeError(f"QoderWake 守护进程起不来（退出码 {proc.poll()}）")


def wake_cookie(port):
    """用 QoderWake 给本机 CLI 留下的明文 token 换一个临时会话。"""
    token_file = WAKE_DIR / ".auth" / "token"
    if not token_file.exists():
        raise RuntimeError("缺少 ~/.qoderwake-cn/.auth/token，请先打开 QoderWake CN 并登录")
    _, hdrs, _ = http(
        f"http://127.0.0.1:{port}/api/frontend-auth/session/bootstrap",
        method="POST", body={},
        headers={"Authorization": f"Bearer {token_file.read_text().strip()}",
                 "X-QoderWake-Frontend-Session-Client": "cli",
                 "Content-Type": "application/json"})
    cookie = hdrs.get("set-cookie", "").split(";")[0]
    if not cookie:
        raise RuntimeError("QoderWake 拒绝签发本机会话（多半是它自己掉登录了）")
    return cookie


def wake_get(port, cookie, path):
    code, _, body = http(f"http://127.0.0.1:{port}{path}", headers={"Cookie": cookie})
    if code != 200:
        raise RuntimeError(f"{path} 返回 HTTP {code}: {str(body)[:160]}")
    if isinstance(body, dict) and not body.get("success", True):
        raise RuntimeError(f"{path} 失败: {str(body)[:160]}")
    return body.get("data", body) if isinstance(body, dict) else body


def wake_post(port, cookie, path):
    code, _, body = http(f"http://127.0.0.1:{port}{path}", method="POST",
                         body={}, headers={"Cookie": cookie, "Content-Type": "application/json"})
    return code, body if isinstance(body, dict) else {"raw": str(body)[:200]}


def qoder_inventory(usage):
    q = usage.get("qoderUsage") or {}
    add, plan = q.get("addOnQuota") or {}, q.get("userQuota") or {}
    if not add:
        inventory_add("qoder", "Qoder", "/Applications/Qoder CN.app",
                      note="余额读不到（接口没给加油包）")
        return
    total, paid = add.get("total") or 0, plan.get("total") or 0
    inventory_add("qoder", "Qoder", "/Applications/Qoder CN.app",
                  unit="分", total=total, remaining=add.get("remaining"),
                  purchased=paid, gifted=max(total - paid, 0), used=add.get("used"),
                  note=f"套餐额度 {paid} · 加油包 {add.get('remaining')}/{total}")


def inventory_qoder():
    """给菜单栏实时刷新用：只读余额，不领取。"""
    try:
        port, started = ensure_wake()
        try:
            cookie = wake_cookie(port)
            usage = wake_get(port, cookie, "/api/user/usage")
        finally:
            if started:
                quit_wake()
        qoder_inventory(usage)
    except Exception as e:
        inventory_add("qoder", "Qoder", "/Applications/Qoder CN.app", note=f"余额读不到（{e}）")


def collect_qoder(port, cookie):
    """领 Qoder 活动积分，返回这一家的结果条目 {name, state, detail}。"""
    fresh, replay, notes = [], [], []

    def claim(cid, key, source):
        code, body = wake_post(port, cookie, f"/api/user/campaigns/{cid}/claim")
        d = body.get("data") or {}
        benefit = d.get("benefit") or {}
        kind, amount = benefit.get("kind"), benefit.get("amount")
        if body.get("success") and d.get("status") == "CLAIMED":
            word = "已领过" if d.get("replayed") else "补领"
            (replay if d.get("replayed") else fresh).append(f"{kind}×{amount}")
            log(f"[Qoder] {key}（{source}）→ {word} {kind} {amount}")
            if kind == "CREDITS" and d.get("expiresAt"):
                LEDGER_LINES.append({"type": "grant", "service": "Qoder",
                                     "grant_id": d.get("grantId"), "amount": amount,
                                     "claimed_at": d.get("claimedAt"), "expires_at": d.get("expiresAt")})
            return True
        log(f"[Qoder] {key}（{source}）领取未成功: {str(body)[:180]}")
        notes.append(f"{key} 领取被拒")
        return False

    data = wake_get(port, cookie, "/api/user/campaigns")
    campaigns = data.get("campaigns") or []
    for c in campaigns:
        status = str(c.get("claimStatus") or "").upper()
        cid = c.get("campaignId")
        if cid and status not in QODER_DONE:
            claim(cid, c.get("campaignKey"), "Wake 列表")
    # Wake 的列表按客户端类型把每日 100 积分那条藏起来了（实测），只能从桌面端日志拿当天编号
    if not any((c.get("benefit") or {}).get("kind") == "CREDITS" for c in campaigns):
        hit = qoder_round_fresh() or qoder_round_after_waking_app()
        if not hit:
            notes.append("没拿到当天活动编号")
        else:
            # 即使显示已领过也请求一次：幂等（实测 replayed:true 不重复发放），
            # 为的是把到期时间记进台账——你手动领的那几天也要能看到
            claim(hit[0], hit[1], "桌面端")

    # 另一套活动（如周年庆送免费调用），今天查是空的，有货就顺手领
    try:
        acts = (wake_get(port, cookie, "/api/user/activity") or {}).get("activities") or []
    except RuntimeError as e:
        log(f"[Qoder] 活动列表跳过: {e}")
        acts = []
    for a in acts:
        if str(a.get("grantStatus") or a.get("status") or "").upper() in QODER_DONE:
            continue
        aid = a.get("activityId") or a.get("id")
        if aid:
            code, body = wake_post(port, cookie, f"/api/user/activity/claim?activityId={aid}")
            log(f"[Qoder] 额外活动 {aid} → HTTP {code} {str(body)[:160]}")
            if body.get("success"):
                fresh.append("额外活动")

    try:
        usage = wake_get(port, cookie, "/api/user/usage")
        q = usage.get("qoderUsage") or {}
        add, plan = q.get("addOnQuota") or {}, q.get("userQuota") or {}
        if add:
            notes.append(f"余额 {add.get('remaining')}/{add.get('total')}")
            LEDGER_LINES.append({"type": "balance", "service": "Qoder",
                                 "date": f"{datetime.now():%Y-%m-%d}", "add_on": add, "plan": plan})
        qoder_inventory(usage)
    except RuntimeError as e:
        notes.append(f"余额读不到({e})")
        inventory_add("qoder", "Qoder", "/Applications/Qoder CN.app", note=f"余额读不到({e})")

    balance = next((n for n in notes if n.startswith("余额")), "")
    if fresh:
        state, detail = "claimed", "、".join(fresh)
    elif replay:
        state, detail = "already", "、".join(replay)
    else:
        state, detail = "none", "、".join(n for n in notes if not n.startswith("余额"))
    if balance:
        detail = f"{detail}（{balance}）" if detail else balance
    return {"name": "Qoder", "state": state, "detail": detail or "今天没有可领的活动"}


QODER_APP = Path("/Applications/Qoder CN.app")
QODER_MAIN_BIN = "/Applications/Qoder CN.app/Contents/MacOS/Qoder CN"


def qoder_main_pid():
    """只认桌面端主进程（参数里带 app.asar 的那个），插件宿主和 Helper 不算。"""
    out = subprocess.run(["ps", "-axo", "pid=,args="], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if QODER_MAIN_BIN in line and "app.asar" in line:
            return int(line.split()[0])
    return None


def qoder_credits_round():
    """桌面端日志里当天领取窗口内的积分活动，返回 (id, key) 或 None。"""
    log_root = APP_BASE / "com.qodercn.app.stable" / "logs"
    if not log_root.exists():
        return None
    now = time.time()
    runs = sorted((p for p in log_root.iterdir() if p.is_dir()),
                  key=lambda p: p.name, reverse=True)[:2]
    for run in runs:
        for name in ("main.log", "main.1.log"):
            f = run / name
            if not f.exists():
                continue
            try:
                lines = f.read_text(errors="ignore").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                i = line.find("活动状态请求返回")
                if i < 0:
                    continue
                j = line.find("{", i)
                try:
                    payload = json.loads(line[j:]).get("payload") or {}
                except (ValueError, TypeError):
                    continue
                for c in payload.get("campaigns") or []:
                    if (c.get("benefit") or {}).get("kind") != "CREDITS":
                        continue
                    if not (c.get("startAt", 0) <= now < c.get("endAt", 9e18)):
                        continue
                    return c.get("campaignId"), c.get("campaignKey")
                break
    return None


def qoder_cache_round():
    """桌面端 2026-10-23 起不再把活动请求写进 main.log（与版本无关，0.4.9 起就这样），
    但 /me/campaigns 的响应仍留在 Chromium 磁盘缓存里。只看这一轮开始之后写过的缓存文件，不翻历史。"""
    cache = APP_BASE / "com.qodercn.app.stable" / "Cache" / "Cache_Data"
    if not cache.exists():
        return None
    now = time.time()
    start = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    if start.timestamp() > now:
        start -= timedelta(days=1)
    try:
        files = sorted((p for p in cache.iterdir() if p.is_file()),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for f in files:
        try:
            if f.stat().st_mtime < start.timestamp() - 60 or f.stat().st_size > 8_000_000:
                continue
            raw = f.read_bytes().decode("utf-8", "ignore")
        except OSError:
            continue
        i = raw.find("/sash/api/v1/me/campaigns{")
        if i < 0:
            continue
        j = raw.find("{", i)
        try:
            payload = json.JSONDecoder().raw_decode(raw[j:])[0]
        except ValueError:
            continue
        for c in payload.get("campaigns") or []:
            if (c.get("benefit") or {}).get("kind") != "CREDITS":
                continue
            if c.get("startAt", 0) <= now < c.get("endAt", 9e18):
                return c.get("campaignId"), c.get("campaignKey")
    return None


def qoder_round_fresh():
    """日志优先（老版本还有），其次新版留下的 Chromium 缓存。"""
    return qoder_credits_round() or qoder_cache_round()


def wait_for_round(timeout=720, step=20):
    """桌面端刷新活动是慢的，10:01 常比它早；在跑就等它写进日志或缓存，没跑就立刻返回。"""
    if not qoder_main_pid():
        return None
    started = time.time()
    deadline = started + timeout
    announced = False
    while True:
        hit = qoder_round_fresh()
        if hit:
            if announced:
                log(f"[Qoder] 等了 {int(time.time() - started)} 秒，桌面端刷新出了 {hit[1]}")
            return hit
        if time.time() >= deadline:
            log(f"[Qoder] 等满 {timeout // 60} 分钟，桌面端仍没刷新出今天的活动")
            return None
        if not announced:
            log(f"[Qoder] 桌面端还没刷新出今天的活动，最多等 {timeout // 60} 分钟")
            announced = True
        time.sleep(step)


def qoder_round_after_waking_app():
    """桌面端今天没跑过时，静默拉起它等它把当天活动写进日志，用完再退出。

    先试 -j 完全隐藏启动；20 秒没等到再试 -g（窗口会出现一下）。
    使用者自己在用的时候绝不开也绝不关。
    """
    if not QODER_APP.exists():
        log("[Qoder] 没找到 Qoder CN.app，无法补拿活动编号")
        return None
    if qoder_main_pid():
        log("[Qoder] 桌面端正在运行，但日志里没有当天窗口内的积分活动")
        return None
    for flags in (["-g", "-j"], ["-g"]):
        subprocess.run(["open", *flags, str(QODER_APP)], check=False)
        pid = qoder_main_pid()
        for _ in range(10):
            time.sleep(2)
            hit = qoder_round_fresh()
            if hit:
                log(f"[Qoder] 用 open {' '.join(flags)} 静默拉起桌面端拿到了当天活动")
                break
        else:
            hit = None
        if hit:
            if pid and pid == qoder_main_pid():
                STARTED_PIDS["qoder"] = [pid] + pids_matching("Qoder CN.app/Contents/")
                quit_app(STARTED_PIDS["qoder"], "Qoder 桌面端")
            else:
                log("[Qoder] 桌面端进程号变了（使用者可能自己开了），不动它")
            return hit
    log("[Qoder] 拉起桌面端 40 秒仍没拿到当天活动")
    return None


# ---------------------------------------------------------------- Trae CN
APP_BASE = HOME / "Library" / "Application Support"
TRAE_DIRS = ["Trae CN", "TRAE SOLO CN"]
TRAE_AUTH_KEY = "iCubeAuthInfo://icube.cloudide"
TRAE_API = "https://api.trae.cn/trae/api/v2/ug/checkin_credits"
# 逆向自官方客户端的 iCube 登录态加密常量
URE = bytes([82, 9, 106, 213, 48, 54, 165, 56, 191, 64, 163, 158, 129, 243, 215, 251,
             124, 227, 57, 130, 155, 47, 255, 135, 52, 142, 67, 68, 196, 222, 233, 203,
             84, 123, 148, 50, 166, 194, 35, 61, 238, 76, 149, 11, 66, 250, 195, 78, 8,
             46, 161, 102, 40, 217, 36, 178, 118, 91, 162, 73, 109, 139, 209, 37])
DRE = bytes([31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39, 128, 236, 95,
             96, 81, 127, 169, 25, 181, 74, 13, 45, 229, 122, 159, 147, 201, 156, 239,
             160, 224, 59, 77, 174, 42, 245, 176, 200, 235, 187, 60, 131, 83, 153, 97,
             23, 43, 4, 126, 186, 119, 214, 38, 225, 105, 20, 99, 85, 33, 12, 125])


def trae_token():
    """从任一 Trae 客户端的本地登录态里解出可用 token。"""
    for name in TRAE_DIRS:
        sf = APP_BASE / name / "User" / "globalStorage" / "storage.json"
        if not sf.exists():
            continue
        try:
            enc = json.loads(sf.read_text(encoding="utf-8")).get(TRAE_AUTH_KEY)
            if not enc:
                log(f"[Trae] {name} 无登录态")
                continue
            t = base64.b64decode(enc)
            key = t[6:38]
            h = hashlib.sha512(hashlib.sha512(key).digest()
                               + bytes(a ^ b for a, b in zip(URE, DRE))).digest()
            plain = unpad(AES.new(h[:16], AES.MODE_CBC, h[16:32])
                          .decrypt(t[38:]), AES.block_size)
            auth = json.loads(plain[64:].decode("utf-8"))
            if auth.get("token"):
                return name, auth
        except Exception as e:
            log(f"[Trae] {name} 登录态解析失败: {e}")
    return None, None


def trae_device_id():
    for name in TRAE_DIRS:
        d = APP_BASE / name
        p = d / "ahanet" / "tt_net_config.config"
        if p.exists():
            import re
            m = re.search(r"device_id&#\*(\d+)", p.read_text(errors="ignore"))
            if m:
                return m.group(1)
        m = d / "machineid"
        if m.exists() and m.read_text().strip():
            return m.read_text().strip()
    return ""


def trae_api(url, token, region, device_id):
    headers = {"Authorization": f"Cloud-IDE-JWT {token}", "Content-Type": "application/json",
               "x-device-id": device_id}
    if region:
        headers["X-User-Region"] = region
    _, _, body = http(url, method="POST", headers=headers, body={"req_source": 1})
    return body.get("data", body) if isinstance(body, dict) and "data" in body else body


def trae_inventory(st):
    credits = st.get("credits") or 0
    inventory_add("trae", "Trae CN", "/Applications/Trae CN.app",
                  unit="分", total=credits, remaining=credits, purchased=0,
                  gifted=credits, used=0, quota_known=False,
                  expires="接口未提供有效期",
                  note=f"额外 {st.get('extra_credits', 0)}")


def inventory_trae():
    """给菜单栏实时刷新用：只读余额，不签到。"""
    try:
        name, auth = trae_token()
        if not auth:
            raise RuntimeError("没有可用登录态")
        st = trae_api(f"{TRAE_API}/status", auth["token"],
                      (auth.get("userRegion") or {}).get("region"), trae_device_id())
        trae_inventory(st)
    except Exception as e:
        inventory_add("trae", "Trae CN", "/Applications/Trae CN.app", note=f"读不到（{e}）")


def collect_trae():
    """Trae 每日签到，返回 {name, state, detail}。"""
    name, auth = trae_token()
    if not auth:
        inventory_add("trae", "Trae CN", "/Applications/Trae CN.app",
                      note="余额读不到（没有可用登录态，打开一次 Trae）")
        return {"name": "Trae", "state": "error", "detail": "没有可用登录态，打开一次 Trae 登录"}
    dev = trae_device_id()
    region = (auth.get("userRegion") or {}).get("region")
    tok = auth["token"]

    def call(path):
        return trae_api(f"{TRAE_API}/{path}", tok, region, dev)

    try:
        st = call("status")
    except Exception as e:
        inventory_add("trae", "Trae CN", "/Applications/Trae CN.app", note=f"网络错误({e})")
        return {"name": "Trae", "state": "error", "detail": f"网络错误({e})"}
    trae_inventory(st)
    def book():
        LEDGER_LINES.append({"type": "balance", "service": "Trae",
                             "date": f"{datetime.now():%Y-%m-%d}", "credits": st.get("credits")})

    if st.get("checked_in"):
        log(f"[Trae] {name} 今日已签到，积分 {st.get('credits')}")
        book()
        return {"name": "Trae", "state": "already", "detail": f"今日已签到（{st.get('credits')} 分）"}
    if not st.get("enable"):
        if st.get("code") == 1001:
            return {"name": "Trae", "state": "error", "detail": "登录态过期，打开一次 Trae 重新登录"}
        return {"name": "Trae", "state": "none", "detail": "签到不可用"}
    try:
        claim = call("claim")
    except Exception as e:
        return {"name": "Trae", "state": "error", "detail": f"领取失败({e})"}
    if claim.get("code") != 0:
        return {"name": "Trae", "state": "error",
                "detail": f"领取失败：{claim.get('message') or str(claim)[:100]}"}
    try:
        after = call("status").get("credits")
    except Exception:
        after = "?"
    log(f"[Trae] {name} 签到成功，积分 {after}")
    LEDGER_LINES.append({"type": "balance", "service": "Trae",
                         "date": f"{datetime.now():%Y-%m-%d}", "credits": after})
    return {"name": "Trae", "state": "claimed", "detail": f"签到成功（余额 {after}）"}


# ---------------------------------------------------------------- MiniMax Code
MINIMAX_AUTH = HOME / ".minimax" / "auth" / "prod" / "cn" / "mcode-public" / "auth.json"
MINIMAX_STATE = MINIMAX_AUTH.parent / "auth-state.json"
MINIMAX_CONFIG = HOME / "Library" / "Application Support" / "MiniMax" / "minimax-agent-cn-config.json"
MINIMAX_API = "https://agent.minimax.cn"
MINIMAX_TOKEN_URL = "https://account.minimax.cn/oauth2/token"
MINIMAX_UA = ("MiniMaxAgent Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) MiniMax/3.1.4 "
              "Chrome/148.0.7778.280 Safari/537.36")
MINIMAX_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "User-Agent": MINIMAX_UA,
    "Origin": "https://agent.minimax.cn",
    "Referer": "https://agent.minimax.cn/",
}


def _write_private(path, text):
    """同目录临时文件 + 原子替换，权限保持 0600（App 会检查权限，松了它就不认）。"""
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    os.chmod(path, 0o600)


@contextmanager
def _minimax_lock(timeout=15):
    """MiniMax 自己用 proper-lockfile：锁是 auth.lock 旁边的同名目录。照它的方式排队。"""
    lockdir = Path(str(MINIMAX_AUTH.parent / "auth.lock") + ".lock")
    end = time.time() + timeout
    while True:
        try:
            lockdir.mkdir(mode=0o700)
            break
        except FileExistsError:
            try:
                if time.time() - lockdir.stat().st_mtime > 30:
                    lockdir.rmdir()
                    continue
            except OSError:
                pass
            if time.time() > end:
                raise RuntimeError("MiniMax 登录文件被占用（App 正在写入），稍后再试")
            time.sleep(0.2)
    try:
        yield
    finally:
        try:
            lockdir.rmdir()
        except OSError:
            pass


def minimax_token():
    """读本地 OAuth 凭据。快过期就在锁内续期，并先把凭据和状态一起写回再返回。"""
    if not MINIMAX_AUTH.exists():
        raise RuntimeError("没找到 MiniMax Code 登录文件，打开一次它登录")
    with _minimax_lock():
        data = json.loads(MINIMAX_AUTH.read_text(encoding="utf-8"))
        records = data.get("records") or {}
        if not records:
            raise RuntimeError("MiniMax Code 没有已登录账号")
        key, rec = next(iter(records.items()))
        if rec.get("expiresAtMs", 0) - time.time() * 1000 > 5 * 60 * 1000:
            return rec["accessToken"]

        form = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": rec.get("refreshToken", ""),
            "client_id": "mcode-public",
            "scope": "agent.default",
            "audience": "agent-backend",
        }).encode()
        req = urllib.request.Request(
            MINIMAX_TOKEN_URL, data=form, method="POST",
            headers={"Accept": "application/json",
                     "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with NO_PROXY.open(req, timeout=25) as resp:
                grant = json.loads(resp.read() or b"{}")
        except (urllib.error.HTTPError, ValueError):
            raise RuntimeError("MiniMax Code 登录态失效，打开 App 重新登录")
        if not grant.get("access_token") or not grant.get("expires_in"):
            raise RuntimeError("MiniMax Code 续期没拿到新 token")

        try:
            state = json.loads(MINIMAX_STATE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        generation = max(int(rec.get("generation") or 0), int(state.get("generation") or 0)) + 1
        rec.update({
            "accessToken": grant["access_token"],
            "refreshToken": grant.get("refresh_token") or rec["refreshToken"],
            "tokenType": "Bearer",
            "generation": generation,
            "expiresAtMs": int(time.time() * 1000) + int(grant["expires_in"]) * 1000,
        })
        data["records"][key] = rec
        _write_private(MINIMAX_AUTH, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        state.update({"status": "authenticated", "generation": generation,
                      "expiresAtMs": rec["expiresAtMs"]})
        _write_private(MINIMAX_STATE, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        log("[MiniMax] 登录态快过期，已续期并把新凭据写回")
        return rec["accessToken"]


def minimax_query():
    uid = ""
    try:
        uid = (json.loads(MINIMAX_CONFIG.read_text(encoding="utf-8")).get("sharedUser") or {}).get("realUserID") or ""
    except (OSError, ValueError):
        pass
    q = {
        "device_platform": "web", "biz_id": 3, "app_id": 3001, "version_code": 22201,
        "unix": int(time.time() * 1000), "timezone_offset": 28800, "is_desktop": 1,
        "desktop_version": "3.1.4", "sys_language": "zh", "lang": "zh", "client": "desktop",
    }
    if uid:
        q["user_id"] = uid
    return urllib.parse.urlencode(q)


def minimax_call(token, method, path, body=None, raw=False):
    code, _, data = http(f"{MINIMAX_API}{path}?{minimax_query()}", method=method,
                         headers={"Authorization": f"Bearer {token}", **MINIMAX_HEADERS},
                         body=body)
    if code != 200 or not isinstance(data, dict):
        raise RuntimeError(f"{path} 返回 HTTP {code}")
    base = data.get("base_resp") or {}
    if base.get("status_code") not in (0, None):
        raise RuntimeError(f"{path} 失败：{base.get('status_msg')}")
    return data if raw else (data.get("data") or {})


def minimax_inventory(token):
    try:
        info = minimax_call(token, "POST", "/matrix/api/v1/commerce/get_membership_info",
                            body={}, raw=True)
        s = info.get("op_credit_summary") or {}
        def num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        inventory_add("minimax", "MiniMax Code", "/Applications/MiniMax Code.app",
                      unit="分", total=num(s.get("total_remaining_amount")),
                      remaining=num(s.get("total_remaining_amount")),
                      purchased=num(s.get("purchased_remaining_amount")),
                      gifted=num(s.get("free_remaining_amount")), used=0,
                      quota_known=False,
                      note=f"购买剩余 {s.get('purchased_remaining_amount', '—')} · "
                           f"免费剩余 {s.get('free_remaining_amount', '—')}")
    except Exception:
        inventory_add("minimax", "MiniMax Code", "/Applications/MiniMax Code.app", note="余额读不到")


def inventory_minimax():
    """给菜单栏实时刷新用：只读余额，不签到。"""
    try:
        token = minimax_token()
    except RuntimeError as e:
        inventory_add("minimax", "MiniMax Code", "/Applications/MiniMax Code.app", note=str(e))
        return
    minimax_inventory(token)


def collect_minimax():
    """MiniMax Code 每日签到，返回 {name, state, detail}。"""
    try:
        token = minimax_token()
    except RuntimeError as e:
        inventory_add("minimax", "MiniMax Code", "/Applications/MiniMax Code.app", note=str(e))
        return {"name": "MiniMax", "state": "error", "detail": str(e)}
    try:
        panel = minimax_call(token, "GET", "/minimax-cloud/api/v1/signin/status")
    except Exception as e:
        inventory_add("minimax", "MiniMax Code", "/Applications/MiniMax Code.app", note=f"网络错误({e})")
        return {"name": "MiniMax", "state": "error", "detail": f"网络错误({e})"}
    minimax_inventory(token)

    today = next((d for d in panel.get("days") or [] if d.get("is_today")), None)
    if not today:
        return {"name": "MiniMax", "state": "error", "detail": "签到面板里没有今天"}
    day_no = today.get("day_no")
    if today.get("status") == 3:
        log(f"[MiniMax] 今日已签到（第 {day_no} 天）")
        return {"name": "MiniMax", "state": "already", "detail": f"今日已签到（第 {day_no} 天）"}
    if today.get("status") != 1:
        return {"name": "MiniMax", "state": "none", "detail": f"今天不可领（第 {day_no} 天，状态 {today.get('status')}）"}

    try:
        claim = minimax_call(token, "POST", "/minimax-cloud/api/v1/signin/claim", body={})
    except Exception as e:
        return {"name": "MiniMax", "state": "error", "detail": f"领取失败({e})"}
    if claim.get("claim_result") == 2:
        return {"name": "MiniMax", "state": "already", "detail": "今天已经签过"}
    if claim.get("claim_result") != 1:
        return {"name": "MiniMax", "state": "error", "detail": f"领取失败：{str(claim)[:100]}"}

    points = claim.get("points")
    log(f"[MiniMax] 第 {claim.get('day_no')} 天签到成功，+{points} 分")
    exp = claim.get("expire_at_ms")
    if exp:
        LEDGER_LINES.append({
            "type": "grant", "service": "MiniMax", "amount": points,
            "grant_id": f"minimax-{claim.get('claim_id')}",
            "claimed_at": f"{datetime.utcnow():%Y-%m-%dT%H:%M:%SZ}",
            "expires_at": datetime.utcfromtimestamp(exp / 1000).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    return {"name": "MiniMax", "state": "claimed", "detail": f"签到成功（第 {claim.get('day_no')} 天 +{points} 分）"}


# ---------------------------------------------------------------- 汇总
def write_status(items):
    """人看的写 最新状态.txt，菜单栏 App 读 今日状态.json。"""
    now = datetime.now()
    text = "；".join(f"{i['name']} {WORDS.get(i['state'], i['state'])}：{i['detail']}"
                     for i in items) or "没有可领的东西"
    _write_atomic(STATUS_FILE, f"[{now:%Y-%m-%d %H:%M}] {text}\n")
    _write_atomic(STATUS_JSON, json.dumps(
        {"date": f"{now:%Y-%m-%d}", "ran_at": f"{now:%H:%M}", "items": items},
        ensure_ascii=False, indent=1))
    log(f"结果：{text}")
    if LEDGER_LINES:
        seen = set()
        if LEDGER_FILE.exists():
            for raw in LEDGER_FILE.read_text(encoding="utf-8").splitlines():
                try:
                    seen.add(json.loads(raw).get("grant_id"))
                except ValueError:
                    continue
        with open(LEDGER_FILE, "a", encoding="utf-8") as f:
            for line in LEDGER_LINES:
                if line.get("grant_id") and line["grant_id"] in seen:
                    continue          # 同一笔重复领取不重复记账
                f.write(json.dumps(line, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- Kimi / ZCode 余额（只读，不签到）
KIMI_CFG = HOME / "Library" / "Application Support" / "kimi-desktop" / "daimon-share" / "daimon" / "config.json"
KIMI_REFRESH_URL = "https://auth.kimi.com/api/account.gateway.v1.AuthService/RefreshToken"
KIMI_API = "https://www.kimi.com/apiv2/kimi.gateway.membership.v2.MembershipService"
ZCODE_LS = HOME / "Library" / "Application Support" / "ZCode" / "session" / "Local Storage" / "leveldb"


def http_kimi(url, method="GET", headers=None, body=None, timeout=20):
    """先走系统代理，不通再直连：Kimi 直连偶尔 TLS 超时。"""
    data = json.dumps(body).encode() if body is not None else None
    last = None
    for opener in (PROXIED, NO_PROXY):
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                code = resp.status
            try:
                return code, json.loads(raw)
            except ValueError:
                return code, raw
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
    raise RuntimeError(f"连不上 Kimi（{last}）")


def _jwt_exp(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0)
    except (IndexError, ValueError):
        return 0


def kimi_token():
    """Kimi Code 的凭据是明文 JSON。access token 只有 15 分钟，快过期就续期并写回。"""
    if not KIMI_CFG.exists():
        raise RuntimeError("没找到 Kimi 的登录文件，打开一次 Kimi")
    cfg = json.loads(KIMI_CFG.read_text(encoding="utf-8"))
    cred = (cfg.get("credentials") or {}).get("kimiWeb") or {}
    if not cred.get("accessToken"):
        raise RuntimeError("Kimi Code 没有登录凭据")
    if _jwt_exp(cred["accessToken"]) - time.time() > 300:
        return cred["accessToken"]
    headers = {"Content-Type": "application/json", "Connect-Protocol-Version": "1",
               "x-msh-platform": "mac", "x-msh-version": "3.2.20",
               "R-Timezone": "Asia/Shanghai", "x-language": "zh-CN"}
    code, data = http_kimi(KIMI_REFRESH_URL, method="POST", headers=headers,
                           body={"refreshToken": cred.get("refreshToken", "")})
    if code != 200 or not isinstance(data, dict) or not data.get("accessToken"):
        raise RuntimeError("Kimi 登录态续期失败，打开一次 Kimi")
    cred["accessToken"] = data["accessToken"]
    if data.get("refreshToken"):
        cred["refreshToken"] = data["refreshToken"]
    cred["updatedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    cfg.setdefault("credentials", {})["kimiWeb"] = cred
    _write_private(KIMI_CFG, json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    log("[Kimi] 登录态快过期，已续期并写回")
    return cred["accessToken"]


def kimi_call(token, name):
    headers = {"Content-Type": "application/json", "Connect-Protocol-Version": "1",
               "Authorization": f"Bearer {token}", "x-msh-platform": "mac",
               "x-msh-version": "3.2.20", "R-Timezone": "Asia/Shanghai", "x-language": "zh-CN"}
    code, data = http_kimi(f"{KIMI_API}/{name}", method="POST", headers=headers, body={})
    if code != 200 or not isinstance(data, dict):
        raise RuntimeError(f"{name} 返回 HTTP {code}")
    return data


def inventory_kimi():
    try:
        token = kimi_token()
        stats = kimi_call(token, "GetSubscriptionStats")
        sub = kimi_call(token, "GetSubscription")
        bal = stats.get("subscriptionBalance") or {}
        plan = ((sub.get("subscription") or {}).get("goods") or {}).get("title") or "?"
        end = ((sub.get("subscription") or {}).get("currentEndTime") or "")[:10]
        ratio = bal.get("amountUsedRatio")
        used_pct = round(ratio * 100) if isinstance(ratio, (int, float)) else None
        if used_pct is None:
            inventory_add("kimi", "Kimi", "/Applications/Kimi.app",
                          expires=f"套餐到 {_cn_date(end)}" if end else "接口未提供有效期",
                          note=f"{plan} 套餐；接口没给使用比例")
        else:
            inventory_add("kimi", "Kimi", "/Applications/Kimi.app",
                          unit="%", total=100, remaining=100 - used_pct,
                          purchased=0, gifted=100, used=used_pct,
                          expires=f"套餐到 {_cn_date(end)}" if end else "接口未提供有效期",
                          note=f"{plan} 套餐；接口只给已用比例，没有绝对分数")
    except Exception as e:
        inventory_add("kimi", "Kimi", "/Applications/Kimi.app", note=f"读不到（{e}）")


def zcode_quota():
    if not ZCODE_LS.exists():
        return None
    dec = json.JSONDecoder()
    best = None
    for f in ZCODE_LS.iterdir():
        if not f.is_file():
            continue
        try:
            raw = f.read_bytes().decode("utf-8", "ignore")
        except OSError:
            continue
        idx = 0
        while True:
            i = raw.find('"periodEnd"', idx)
            if i < 0:
                break
            idx = i + 1
            for j in range(i, max(-1, i - 3000), -1):
                if raw[j] != "{":
                    continue
                try:
                    obj, _ = dec.raw_decode(raw[j:])
                except ValueError:
                    continue
                if isinstance(obj, dict) and "periodEnd" in obj:
                    if best is None or obj.get("periodEnd", 0) > best.get("periodEnd", 0):
                        best = obj
                    break
    return best


def _token_amount(n):
    if n >= 100_000_000:
        return f"{n / 100_000_000:g} 亿"
    if n >= 10_000:
        return f"{n / 10_000:g} 万"
    return str(n)


def inventory_zcode():
    try:
        q = zcode_quota()
        if not q:
            raise RuntimeError("本机还没有额度缓存，打开一次 ZCode")
        unit = q.get("unit") or q.get("number") or 0
        used = q.get("usage") or 0
        remain = q.get("remaining")
        if remain is None:
            remain = unit - used
        end = q.get("periodEnd")
        when = time.strftime("%m-%d %H:%M", time.localtime(end / 1000)) if end else "?"
        period = q.get("period") or ""
        label = {"one_time": "一次性", "daily": "每日", "weekly": "每周", "monthly": "每月"}.get(period, period)
        cycle = f"有效期到 {when}" if period == "one_time" else f"{label}重置 {when}"
        expired = "（缓存已过期，打开 ZCode 刷新）" if end and end / 1000 < time.time() else ""
        inventory_add("zcode", "ZCode", "/Applications/ZCode.app",
                      unit="token", total=unit, remaining=remain,
                      purchased=0, gifted=unit, used=used,
                      expires=f"有效期到 {_cn_date(when)}" if when != "?" else "接口未提供有效期",
                      note=f"套餐 {q.get('planId', '?')}（{label or '额度'}）；"
                           f"已用 {_token_amount(used)}；{cycle}{expired}")
    except Exception as e:
        inventory_add("zcode", "ZCode", "/Applications/ZCode.app", note=f"读不到（{e}）")


DEEPSEEK_KEY_FILE = BASE_DIR / ".deepseek-key"
# 官方 /user/balance 不给有效期，这行是你 2026-10-24 从 DeepSeek 后台抄来的赠送余额到期时间
DEEPSEEK_GRANTED_EXPIRES = "2026-10-27 18:49:08"


def inventory_deepseek():
    """DeepSeek 余额（官方接口，API Key 存在本机 .deepseek-key，0600、不进 git）。"""
    if not DEEPSEEK_KEY_FILE.exists():
        inventory_add("deepseek", "DeepSeek", "/Applications/DeepSeek Harness.app",
                      note="余额读不到（没有 API Key）")
        return
    key = DEEPSEEK_KEY_FILE.read_text(encoding="utf-8").strip()
    try:
        code, _, data = http("https://api.deepseek.com/user/balance",
                             headers={"Authorization": f"Bearer {key}",
                                      "Accept": "application/json"})
    except Exception as e:
        inventory_add("deepseek", "DeepSeek", "/Applications/DeepSeek Harness.app",
                      note=f"余额读不到（{e}）")
        return
    if code != 200 or not isinstance(data, dict):
        inventory_add("deepseek", "DeepSeek", "/Applications/DeepSeek Harness.app",
                      note=f"余额读不到（HTTP {code}）")
        return
    infos = data.get("balance_infos") or []
    if not infos:
        inventory_add("deepseek", "DeepSeek", "/Applications/DeepSeek Harness.app",
                      note="余额读不到（接口没给余额）")
        return

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    info = next((i for i in infos if i.get("currency") == "CNY"), infos[0])
    total = num(info.get("total_balance"))
    unit = "元" if info.get("currency") == "CNY" else (info.get("currency") or "")
    inventory_add("deepseek", "DeepSeek", "/Applications/DeepSeek Harness.app",
                  unit=unit, total=total, remaining=total,
                  purchased=num(info.get("topped_up_balance")),
                  gifted=num(info.get("granted_balance")), used=0, quota_known=False,
                  expires=f"赠送到 {_cn_date(DEEPSEEK_GRANTED_EXPIRES)}",
                  note=f"充值 {info.get('topped_up_balance')} · "
                       f"赠送 {info.get('granted_balance')}（{info.get('currency')}）")


# ---------------------------------------------------------------- 活动监控
WATCH_FILE = BASE_DIR / "活动提醒.json"
WATCH_PAGES = [
    ("Qoder", "https://docs.qoder.cn/events/100credits"),
    ("Qoder", "https://docs.qoder.cn/events/flashoffer"),
    ("Qoder", "https://qoder.cn/activities"),
    ("Trae", "https://www.trae.cn/"),
    ("MiniMax", "https://www.minimaxi.com/blog"),
    ("Kimi", "https://www.kimi.com/"),
    ("智谱", "https://www.zhipuai.cn/"),
    ("通义", "https://tongyi.aliyun.com/"),
    ("豆包", "https://www.doubao.com/"),
    ("CodeBuddy", "https://copilot.tencent.com/"),
    ("文心快码", "https://aca.bce.baidu.com/doc/COMATE/s/rlnvnio4a"),
]
WATCH_KEY = re.compile(r"积分|额度|免费|赠送|领取|活动|福利|credit", re.I)
WATCH_ACTION = re.compile(r"领|赠|送|免费|活动|福利|优惠|加赠", re.I)


def _watch_lines(url):
    """把页面里像"活动公告"的句子挑出来；前端渲染的页面会挑不到，正常。"""
    raw, last = None, None
    # 直连偶尔被重置，退回系统代理再试一次
    for opener in (NO_PROXY, PROXIED):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": MINIMAX_UA})
            with opener.open(req, timeout=10) as resp:
                raw = resp.read(600_000).decode("utf-8", "ignore")
            break
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
    if raw is None:
        raise last or RuntimeError("抓取不了")
    raw = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw, flags=re.S | re.I)
    text = html_lib.unescape(re.sub(r"<[^>]+>", "\n", raw))
    out = []
    for piece in text.splitlines():
        line = re.sub(r"\s+", " ", piece).strip()
        if not (8 <= len(line) <= 100):
            continue
        if not WATCH_KEY.search(line) or not WATCH_ACTION.search(line):
            continue
        if any(w in line for w in ("首页", "登录", "注册", "解决方案", "合作与生态")):
            continue
        out.append(line)
        if len(out) >= 3:
            break
    return out


def scan_campaigns():
    """抓活动页，跟上次比对；第一次只记基线，不弹提醒。"""
    state = {}
    if WATCH_FILE.exists():
        try:
            state = json.loads(WATCH_FILE.read_text(encoding="utf-8"))
        except ValueError:
            state = {}
    pages = state.setdefault("pages", {})
    items = state.setdefault("items", [])
    known = {i.get("text") for i in items}
    now = datetime.now()
    for source, url in WATCH_PAGES:
        try:
            lines = _watch_lines(url)
        except Exception:
            log(f"[活动] {source} 抓取跳过（网络暂时不通）")
            continue
        hashes = pages.get(url)
        fresh = 0
        for line in lines:
            if line in known:
                continue
            h = hashlib.md5(line.encode("utf-8")).hexdigest()
            if hashes is None or h in hashes:
                continue
            items.append({"source": source, "text": line, "url": url,
                          "found_at": f"{now:%Y-%m-%d %H:%M}"})
            known.add(line)
            fresh += 1
        new_hashes = {hashlib.md5(x.encode("utf-8")).hexdigest() for x in lines}
        pages[url] = sorted(set(hashes or []) | new_hashes)[-500:]
        if fresh:
            log(f"[活动] {source} 发现 {fresh} 条新内容")
    cutoff = f"{(now - timedelta(days=30)):%Y-%m-%d %H:%M}"
    items[:] = [i for i in items if i.get("found_at", "") >= cutoff]
    _write_atomic(WATCH_FILE, json.dumps({"pages": pages, "items": items},
                                         ensure_ascii=False, indent=1))
    return items


LEDGER_NAMES = {"qoder": "Qoder", "trae": "Trae", "minimax": "MiniMax"}


def _merge_last_numbers(new, old):
    """刷新失败时保留上一次的读数，别把菜单栏清空。"""
    if not old or new.get("total") is not None or new.get("remaining") is not None:
        return new
    if old.get("total") is None:
        return new
    for field in ("unit", "total", "remaining", "purchased", "gifted", "used",
                  "quota_known", "expires"):
        new[field] = old.get(field)
    new["note"] = f"上次读数（本次刷新失败：{new.get('note') or '未知错误'}）"
    return new


def write_inventory(campaigns=None):
    """积分盘查：能读到的余额 + 本机台账记录到的领取总数 + 最近的活动。"""
    granted = {}
    expiry = {}
    since = None
    if LEDGER_FILE.exists():
        for raw in LEDGER_FILE.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if d.get("type") == "grant" and d.get("service"):
                svc = d["service"]
                granted[svc] = granted.get(svc, 0) + (d.get("amount") or 0)
                day = (d.get("date") or d.get("claimed_at") or "")[:10]
                if day and (since is None or day < since):
                    since = day
                exp = (d.get("expires_at") or "")[:10]
                if exp and (svc not in expiry or exp < expiry[svc]):
                    expiry[svc] = exp
    for row in INVENTORY:
        if row["claimed"] is None:
            total = granted.get(LEDGER_NAMES.get(row["key"]))
            row["claimed"] = total if total else None
        if not row.get("expires"):
            day = expiry.get(LEDGER_NAMES.get(row["key"]))
            if day:
                row["expires"] = f"最早 {_cn_date(day)} 到期"
            elif row["key"] in ("qoder", "minimax"):
                row["expires"] = "还没记录到到期时间"
    prev = {}
    if INV_FILE.exists():
        try:
            prev = {a.get("key"): a for a in
                    (json.loads(INV_FILE.read_text(encoding="utf-8")).get("apps") or [])}
        except ValueError:
            prev = {}
    INVENTORY[:] = [_merge_last_numbers(row, prev.get(row["key"])) for row in INVENTORY]
    _write_atomic(INV_FILE, json.dumps(
        {"updated_at": f"{datetime.now():%Y-%m-%d %H:%M}", "claimed_since": since or "",
         "apps": INVENTORY, "campaigns": campaigns or []}, ensure_ascii=False, indent=1))


def quit_wake():
    """用完就走，不弹确认、不需人工确认后才做。"""
    quit_app(STARTED_PIDS["wake"], "QoderWake")


def main():
    if not acquire_run_lock(90):
        log("另一轮正在运行，本次跳过")
        return 0
    log("=" * 46)
    started_wake = False
    items = []
    try:
        wait_for_round()   # 放在起 QoderWake 之前：等的这几分钟不用一直占着它的守护进程
        port, started_wake = ensure_wake()
        items.append(collect_qoder(port, wake_cookie(port)))
    except Exception as e:
        log(f"[Qoder] {e}")
        items.append({"name": "Qoder", "state": "error", "detail": str(e)})
        inventory_add("qoder", "Qoder", "/Applications/Qoder CN.app",
                      note=f"余额读不到（{e}）")
    try:
        items.append(collect_trae())
    except Exception as e:
        log(f"[Trae] {e}")
        items.append({"name": "Trae", "state": "error", "detail": str(e)})
        inventory_add("trae", "Trae CN", "/Applications/Trae CN.app",
                      note=f"余额读不到（{e}）")
    try:
        items.append(collect_minimax())
    except Exception as e:
        log(f"[MiniMax] {e}")
        items.append({"name": "MiniMax", "state": "error", "detail": str(e)})
        inventory_add("minimax", "MiniMax Code", "/Applications/MiniMax Code.app",
                      note=f"余额读不到（{e}）")
    finally:
        if started_wake:
            quit_wake()
    inventory_kimi()
    inventory_zcode()
    inventory_deepseek()
    for key, name, app, detail in (
        ("codebuddy", "CodeBuddy", "/Applications/CodeBuddy CN.app", "余额读不到（token 被系统钥匙串加密）"),
        ("doubao", "Doubao", "/Applications/DoubaoWork.app", "余额读不到（没找到接口）"),
        ("opencode", "OpenCode", "/Applications/OpenCode.app", "本地客户端，没有积分"),
    ):
        inventory_add(key, name, app, note=detail)
    write_status(items)
    write_inventory(scan_campaigns())
    return 0


BALANCE_REFRESH = {
    "qoder": inventory_qoder,
    "trae": inventory_trae,
    "minimax": inventory_minimax,
    "kimi": inventory_kimi,
    "zcode": inventory_zcode,
    "deepseek": inventory_deepseek,
}


def refresh_balance(key):
    """只刷新一家余额，合并进 积分盘查.json，不动其他家的数据。"""
    global QUIET
    if not acquire_run_lock():
        return 0
    QUIET = True
    INVENTORY.clear()
    BALANCE_REFRESH[key]()
    if not INVENTORY:
        return 1
    new = INVENTORY[0]
    data = {}
    if INV_FILE.exists():
        try:
            data = json.loads(INV_FILE.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    apps = data.get("apps") or []
    for i, old in enumerate(apps):
        if old.get("key") == key:
            if new.get("claimed") is None:
                new["claimed"] = old.get("claimed")
            if not new.get("expires"):
                new["expires"] = old.get("expires")
            apps[i] = _merge_last_numbers(new, old)
            break
    else:
        apps.append(new)
    data["apps"] = apps
    data["updated_at"] = f"{datetime.now():%Y-%m-%d %H:%M}"
    _write_atomic(INV_FILE, json.dumps(data, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    if "--balance" in sys.argv:
        i = sys.argv.index("--balance")
        key = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
        if key not in BALANCE_REFRESH:
            print("用法: 领积分.py --balance qoder|trae|minimax|kimi|zcode", file=sys.stderr)
            sys.exit(2)
        sys.exit(refresh_balance(key))
    sys.exit(main())
