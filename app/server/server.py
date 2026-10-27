#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
intel-powerctl —— 飞牛 fnOS Intel CPU 功耗控制服务
=================================================
原理（对标 RyzenAdj 的 Intel 等价实现）：
  - RyzenAdj 走的是 AMD SMU，Intel 平台不能直接套用；
  - Intel 的等价机制是 RAPL（Running Average Power Limit）MSR 寄存器：
      MSR 0x606 RAPL_POWER_UNIT   功耗/时间的最小单位
      MSR 0x610 PKG_POWER_LIMIT   PL1(持续功耗墙)/PL2(短时睿频墙)/时间窗 tau
      MSR 0x611 PKG_ENERGY_STATUS 能耗计数器（用于实时功耗计算）
      MSR 0x1FC ENERGY_PERF_BIAS  能效偏好（0=最高性能，15=最省电）
  - 写 /dev/cpu/*/msr 需要 root（config/privilege 已声明 run-as=root）。

功能：
  GET  /api/status  -> 当前 PL1/PL2/tau、实时封装功耗、CPU 温度、频率
  POST /api/apply   -> 应用功耗设置并持久化
  POST /api/preset  -> 应用预设方案（省电/均衡/性能/满血）
  POST /api/reset   -> 恢复到应用启动时记录的原始值

界面：内置 index.html，端口由 --port 指定（默认 9527）。
"""

import argparse
import glob
import json
import os
import socketserver
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ------------------------------------------------------------------
# 配置持久化路径
CONFIG_PATH = "/var/lib/intel-powerctl/config.json"
UI_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "ui"))
# 飞牛网关反代前缀：桌面图标通过 /app/intel-powerctl 转发到 app.sock
GATEWAY_PREFIX = "/app/intel-powerctl"

# ------------------------------------------------------------------
# MSR 寄存器地址（Intel SDM Vol.4 定义）
MSR_RAPL_POWER_UNIT = 0x606   # 功耗/能耗/时间单位
MSR_PKG_POWER_LIMIT = 0x610   # 封装功耗墙 PL1/PL2
MSR_PKG_ENERGY_STATUS = 0x611  # 封装能耗累计计数器
MSR_ENERGY_PERF_BIAS = 0x1FC  # 能效偏好 0~15

MSR_PATHS = sorted(glob.glob("/dev/cpu/[0-9]*/msr"))

# ------------------------------------------------------------------
# 模块级状态：启动时记录原始值，供"恢复默认"使用
_lock = threading.Lock()
_original = None  # {"pl1_w","pl2_w","tau_s","epb","raw_610"}


def _msr_read(msr_path: str, reg: int) -> int:
    """读单个 MSR 寄存器，返回 64 位无符号整数"""
    with open(msr_path, "rb", buffering=0) as f:
        f.seek(reg)
        return struct.unpack("<Q", f.read(8))[0]


def _msr_write(msr_path: str, reg: int, value: int):
    """写单个 MSR 寄存器（64 位）"""
    with open(msr_path, "wb", buffering=0) as f:
        f.seek(reg)
        f.write(struct.pack("<Q", value))


# ------------------------------------------------------------------
# RAPL 单位换算：从 0x606 读出最小单位，全部动态计算避免硬编码出错
def _rapl_units():
    """返回 (瓦特单位, 秒单位)：瓦 = 1/2^pu，秒 = 1/2^tu"""
    v = _msr_read(MSR_PATHS[0], MSR_RAPL_POWER_UNIT)
    pu = v & 0xF          # bits 3:0  功耗单位
    tu = (v >> 16) & 0xF  # bits 19:16 时间单位
    return 2.0 ** (-pu), 2.0 ** (-tu)


def _watts_to_raw(w: float, unit: float) -> int:
    return int(w / unit) & 0x7FFF  # 功耗字段宽度 15 bits


def _raw_to_watts(raw: int, unit: float) -> float:
    return raw * unit


def _tau_to_raw(seconds: float, unit: float) -> int:
    # 时间窗编码：raw = 2^n * time_unit（硬件只支持 2 的幂档位），就近取整
    val = seconds / unit
    n = 0
    best = 0
    best_diff = float("inf")
    for n in range(32):
        diff = abs(2.0 ** n - val)
        if diff < best_diff:
            best, best_diff = n, diff
    return best


def _raw_to_tau(n: int, unit: float) -> float:
    return (2.0 ** n) * unit


# ------------------------------------------------------------------
# 核心读写
def read_power_limit() -> dict:
    """读取当前 PL1/PL2/tau（以 CPU0 为准，所有核共享封装域）"""
    unit, tunit = _rapl_units()
    v = _msr_read(MSR_PATHS[0], MSR_PKG_POWER_LIMIT)
    pl1_raw = v & 0x7FFF
    pl1_on = bool(v & (1 << 15))
    pl2_raw = (v >> 32) & 0x7FFF
    pl2_on = bool(v & (1 << 47))
    tau_n = (v >> 49) & 0x1F
    return {
        "pl1": round(_raw_to_watts(pl1_raw, unit), 1),
        "pl1Enabled": pl1_on,
        "pl2": round(_raw_to_watts(pl2_raw, unit), 1),
        "pl2Enabled": pl2_on,
        "tau": round(_raw_to_tau(tau_n, tunit), 2),
        "tunit": tunit,
        "epb": _msr_read(MSR_PATHS[0], MSR_ENERGY_PERF_BIAS) & 0xF,
    }


def write_power_limit(pl1: float, pl2: float, tau: float, epb: int):
    """写功耗墙到所有 CPU 的 MSR（保留保留位，read-modify-write）"""
    if not MSR_PATHS:
        raise OSError("MSR 设备不可用：请先执行 modprobe msr 并重启本应用")
    unit, tunit = _rapl_units()
    for msr in MSR_PATHS:
        old = _msr_read(msr, MSR_PKG_POWER_LIMIT)
        # 清掉 PL1/PL2/tau 字段，其余保留位原样保留
        keep = old & ~((0x7FFF) | (1 << 15) | (1 << 16) |
                       (0x7FFF << 32) | (1 << 47) | (0x1F << 49))
        new = keep
        new |= _watts_to_raw(pl1, unit)          # PL1
        new |= (1 << 15)                          # PL1 enable
        new |= _watts_to_raw(pl2, unit) << 32     # PL2
        new |= (1 << 47)                          # PL2 enable
        new |= _tau_to_raw(tau, tunit) << 49      # 时间窗
        _msr_write(msr, MSR_PKG_POWER_LIMIT, new)
        # 能效偏好：0~15
        old_epb = _msr_read(msr, MSR_ENERGY_PERF_BIAS)
        _msr_write(msr, MSR_ENERGY_PERF_BIAS, (old_epb & ~0xF) | (epb & 0xF))


def _rapl_energy_unit() -> float:
    """能量单位 = 1/2^EU，EU 取 0x606 bits 12:8（与功率单位不同！）"""
    v = _msr_read(MSR_PATHS[0], MSR_RAPL_POWER_UNIT)
    eu = (v >> 8) & 0x1F
    return 2.0 ** (-eu)


def read_live_power(sample_s=0.3) -> float:
    """两次读能耗计数器求差 => 实时封装功耗（瓦）"""
    unit = _rapl_energy_unit()
    e1 = _msr_read(MSR_PATHS[0], MSR_PKG_ENERGY_STATUS) & 0xFFFFFFFF
    time.sleep(sample_s)
    e2 = _msr_read(MSR_PATHS[0], MSR_PKG_ENERGY_STATUS) & 0xFFFFFFFF
    if e2 < e1:  # 32 位计数器回绕
        e2 += 1 << 32
    return round((e2 - e1) * unit / sample_s, 1)


def read_cpu_temp() -> float:
    """从 coretemp 读最高核温"""
    best = None
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        name_path = os.path.join(hw, "name")
        try:
            if open(name_path).read().strip() != "coretemp":
                continue
            for inp in glob.glob(os.path.join(hw, "temp*_input")):
                t = int(open(inp).read()) / 1000.0
                best = t if best is None else max(best, t)
        except (IOError, ValueError):
            continue
    return round(best, 1) if best is not None else None


def read_freq() -> float:
    """当前全核平均频率（GHz）"""
    try:
        vals = []
        for f in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq"):
            vals.append(int(open(f).read()))
        return round(sum(vals) / len(vals) / 1e6, 2) if vals else None
    except IOError:
        return None


# ------------------------------------------------------------------
# 持久化：保存/恢复配置（应用启动时自动应用上次设置）
def save_config(cfg: dict):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (IOError, ValueError):
        return {}


def capture_original():
    """记录启动时的原始功耗设置，用于一键还原"""
    global _original
    try:
        cur = read_power_limit()
        _original = cur
    except (IOError, OSError) as e:
        print(f"[warn] 记录原始功耗失败: {e}", file=sys.stderr)
        _original = None


def apply_saved_or_defaults():
    """启动时：有保存的配置就应用，否则只记录原始值"""
    if MSR_PATHS:  # 降级模式下没有 MSR 设备，跳过读取
        capture_original()
        cfg = load_config()
        if cfg.get("pl1"):
            try:
                write_power_limit(cfg["pl1"], cfg["pl2"], cfg["tau"], cfg.get("epb", 6))
                print(f"[boot] 已恢复上次配置: PL1={cfg['pl1']}W PL2={cfg['pl2']}W")
            except (IOError, OSError) as e:
                print(f"[boot] 恢复配置失败（可能被 BIOS 锁定）: {e}", file=sys.stderr)


# ------------------------------------------------------------------
# HTTP 服务
class Handler(BaseHTTPRequestHandler):
    def _path(self):
        """去掉网关前缀，兼容 飞牛桌面(/app/intel-powerctl/...) 与 直连端口 两种访问"""
        p = self.path.split("?")[0]
        if p.startswith(GATEWAY_PREFIX):
            p = p[len(GATEWAY_PREFIX):] or "/"
        return p

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path):
        if path == "/":
            path = "/index.html"
        fp = os.path.normpath(os.path.join(UI_DIR, path.lstrip("/")))
        if not fp.startswith(UI_DIR) or not os.path.isfile(fp):
            self.send_response(404)
            self.end_headers()
            return
        ctype = "text/html" if fp.endswith(".html") else (
            "application/javascript" if fp.endswith(".js") else
            "text/css" if fp.endswith(".css") else "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.end_headers()
        with open(fp, "rb") as f:
            self.wfile.write(f.read())

    def do_GET(self):
        if self._path().startswith("/api/status"):
            if not MSR_PATHS:
                return self._json(503, {"ok": False, "msr": False,
                    "error": "MSR 设备不可用：请在 SSH 执行 modprobe msr 后重启本应用"})
            try:
                with _lock:
                    lim = read_power_limit()
                    power = read_live_power()
                self._json(200, {
                    "ok": True, "msr": True,
                    "pl1": lim["pl1"], "pl2": lim["pl2"], "tau": lim["tau"],
                    "epb": lim["epb"],
                    "power": power, "temp": read_cpu_temp(), "freq": read_freq(),
                })
            except (IOError, OSError) as e:
                self._json(500, {"ok": False, "error": f"MSR 读取失败: {e}"})
        elif self._path().startswith("/api/original"):
            self._json(200, {"ok": True, "original": _original})
        else:
            self._static(self._path())

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json(400, {"ok": False, "error": "请求格式错误"})

        if self._path().startswith("/api/apply"):
            if not MSR_PATHS:
                return self._json(503, {"ok": False,
                    "error": "MSR 设备不可用：请在 SSH 执行 modprobe msr 后重启本应用"})
            try:
                pl1 = max(25.0, min(150.0, float(req["pl1"])))
                pl2 = max(25.0, min(200.0, float(req["pl2"])))
                tau = max(0.05, min(448.0, float(req.get("tau", 28))))
                epb = max(0, min(15, int(req.get("epb", 6))))
                with _lock:
                    write_power_limit(pl1, pl2, tau, epb)
                save_config({"pl1": pl1, "pl2": pl2, "tau": tau, "epb": epb})
                return self._json(200, {"ok": True})
            except (IOError, OSError) as e:
                return self._json(500, {"ok": False,
                    "error": f"写入失败（BIOS 可能锁定了功耗墙）: {e}"})

        if self._path().startswith("/api/preset"):
            presets = {
                "省电静音": {"pl1": 35, "pl2": 65, "tau": 8, "epb": 12},
                "均衡推荐": {"pl1": 65, "pl2": 95, "tau": 28, "epb": 6},
                "性能优先": {"pl1": 95, "pl2": 130, "tau": 56, "epb": 2},
                "满血释放": {"pl1": 110, "pl2": 150, "tau": 56, "epb": 0},
            }
            p = presets.get(req.get("name", ""))
            if not p:
                return self._json(400, {"ok": False, "error": "未知预设"})
            if not MSR_PATHS:
                return self._json(503, {"ok": False,
                    "error": "MSR 设备不可用：请在 SSH 执行 modprobe msr 后重启本应用"})
            try:
                with _lock:
                    write_power_limit(p["pl1"], p["pl2"], p["tau"], p["epb"])
                save_config(p)
                return self._json(200, {"ok": True, "applied": p})
            except (IOError, OSError) as e:
                return self._json(500, {"ok": False,
                    "error": f"写入失败（BIOS 可能锁定了功耗墙）: {e}"})

        if self._path().startswith("/api/reset"):
            if not _original:
                return self._json(400, {"ok": False, "error": "没有可还原的原始值"})
            try:
                o = _original
                with _lock:
                    write_power_limit(o["pl1"], o["pl2"], o["tau"], o["epb"])
                os.remove(CONFIG_PATH) if os.path.exists(CONFIG_PATH) else None
                return self._json(200, {"ok": True})
            except (IOError, OSError) as e:
                return self._json(500, {"ok": False, "error": str(e)})

        self._json(404, {"ok": False, "error": "not found"})

    def log_message(self, *args):
        pass  # 关闭访问日志刷屏


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=59500)
    ap.add_argument("--socket", default=None,
                    help="unix socket 路径（飞牛网关反代用）")
    args = ap.parse_args()

    # 降级模式：没有 MSR 设备时服务仍然启动，前端显示错误提示
    # （飞牛上 modprobe msr 后即可正常工作）
    if not MSR_PATHS:
        print("[warn] 找不到 /dev/cpu/*/msr，进入降级模式（仅 UI，无法读写功耗）",
              file=sys.stderr)

    apply_saved_or_defaults()

    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)

    # 飞牛桌面图标走网关 unix socket 反代（与 TCP 端口共享同一 Handler）
    if args.socket:
        class UnixHTTPServer(socketserver.ThreadingMixIn,
                             socketserver.UnixStreamServer):
            daemon_threads = True
        try:
            if os.path.exists(args.socket):
                os.remove(args.socket)
            usrv = UnixHTTPServer(args.socket, Handler)
            t = threading.Thread(target=usrv.serve_forever, daemon=True)
            t.start()
            print(f"[start] 网关 socket 已监听: {args.socket}")
        except OSError as e:
            print(f"[warn] unix socket 监听失败: {e}", file=sys.stderr)

    print(f"[start] intel-powerctl 服务已启动，端口 {args.port}，CPU 数 {len(MSR_PATHS)}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
