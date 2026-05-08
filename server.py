#!/usr/bin/env python3
"""
Hermes Mini App v2 — Optimized Telemetry Dashboard
FastAPI + SSE + Async I/O
"""

import asyncio
import json
import os
import pwd
import re
import signal
import time
import shutil
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime

import psutil
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

# ── Configuration ──────────────────────────────────────────────────

class Config:
    """All config from environment, zero hardcoded values."""
    APP_TOKEN: str = os.getenv("MINI_APP_TOKEN", "")
    OPS_PASSWORD: str = os.getenv("OPS_PASSWORD", "")
    OPS_WHITELIST: set = {
        ip.strip() for ip in os.getenv("OPS_WHITELIST", "127.0.0.1,::1").split(",") if ip.strip()
    }
    HERMES_HOME: str = os.getenv("HERMES_HOME", os.path.expanduser("~/.hermes"))
    HERMES_BIN: str = shutil.which("hermes") or "/home/ubuntu/.local/bin/hermes"
    OCI_CONFIG: str = os.getenv("OCI_CONFIG", os.path.expanduser("~/.oci/config"))
    OCI_ENABLED: bool = os.path.isfile(os.path.expanduser("~/.oci/config"))
    NET_IFACE: str = os.getenv("NET_IFACE", "enp0s6")
    MONTHLY_QUOTA: int = int(os.getenv("MONTHLY_QUOTA", str(10 * 1024 ** 4)))  # 10 TB
    SERVER_HOST: str = os.getenv("SERVER_HOST", "127.0.0.1")
    SERVER_PORT: int = int(os.getenv("SERVER_PORT", "9120"))

    USER: str = os.getenv("SUDO_USER") or os.getenv("USER") or "ubuntu"
    USER_HOME: str = os.path.expanduser(f"~{USER}")

    # Service mapping for restart
    SERVICES: dict = {
        "gateway": (["systemctl", "--user", "restart", "hermes-gateway"], True),
        "nginx": (["sudo", "systemctl", "restart", "nginx"], False),
        "miniapp": (["sudo", "systemctl", "restart", "hermes-mini-app"], False),
        "server": (["sudo", "reboot"], False),
    }

    # Predefined commands for /api/exec/<name>
    ALLOWED_COMMANDS: dict = {
        "hermes_status": ["hermes", "status"],
        "gateway_status": ["hermes", "gateway", "status"],
        "nginx_reload": ["sudo", "systemctl", "reload", "nginx"],
        "df": ["df", "-h"],
        "free": ["free", "-h"],
        "uptime": ["uptime"],
    }


cfg = Config()

# ── FastAPI App ────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield

app = FastAPI(title="Hermes Mini App v2", version="2.0.1", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Serve static files
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ── Helpers ────────────────────────────────────────────────────────

def get_user_env() -> dict:
    """Ensure user-level systemd can find XDG_RUNTIME_DIR."""
    try:
        uid = pwd.getpwnam(cfg.USER).pw_uid
    except KeyError:
        uid = os.getuid()
    return {
        **os.environ,
        "XDG_RUNTIME_DIR": f"/run/user/{uid}",
        "HOME": cfg.USER_HOME,
    }


async def arun(cmd: list[str], **kw) -> tuple[int, str, str]:
    """Async subprocess runner with timeout."""
    timeout = kw.pop("timeout", 15)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **kw,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"Command timed out after {timeout}s: {' '.join(cmd)}")
    return proc.returncode or 0, stdout.decode(), stderr.decode()


def get_client_ip(request: Request) -> str:
    """Extract real client IP behind nginx."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_auth(request: Request) -> str:
    """Validate token from header or query param."""
    token = request.headers.get("X-API-Token", "") or request.query_params.get("token", "")
    if not token or token != cfg.APP_TOKEN:
        raise HTTPException(401, "Unauthorized: invalid token")
    return token


def check_ops(request: Request) -> None:
    """Check IP whitelist or password for ops endpoints."""
    ip = get_client_ip(request)
    if ip in cfg.OPS_WHITELIST:
        return
    data = (request.state.body or {}) if hasattr(request.state, "body") else {}
    password = request.headers.get("X-Ops-Password", "") or data.get("ops_password", "")
    if password and password == cfg.OPS_PASSWORD:
        return
    raise HTTPException(403, f"Access denied from {ip}")


# ── IO Stats Caching ───────────────────────────────────────────────

_io_cache = {"disk": None, "net": None, "time": 0.0}


def _sample_io() -> dict:
    """Sample disk & network I/O, compute rates from previous sample."""
    global _io_cache
    now = time.time()
    d = psutil.disk_io_counters()
    n = psutil.net_io_counters()
    cur = {"disk": d, "net": n, "time": now}

    result = {
        "disk_read_bps": 0, "disk_write_bps": 0,
        "net_up_bps": 0, "net_down_bps": 0,
        "total_sent": n.bytes_sent, "total_recv": n.bytes_recv,
        "disk_read_total": d.read_bytes, "disk_write_total": d.write_bytes,
    }

    prev = _io_cache
    if prev.get("disk") and prev["time"] > 0:
        dt = now - prev["time"]
        if dt > 0:
            result["disk_read_bps"] = round((d.read_bytes - prev["disk"].read_bytes) / dt)
            result["disk_write_bps"] = round((d.write_bytes - prev["disk"].write_bytes) / dt)
            result["net_up_bps"] = round((n.bytes_sent - prev["net"].bytes_sent) / dt)
            result["net_down_bps"] = round((n.bytes_recv - prev["net"].bytes_recv) / dt)

    _io_cache = cur
    return result


# ── Metrics Collector ──────────────────────────────────────────────

def collect_system() -> dict:
    """Single-pass system metric collection."""
    cpu_percent = psutil.cpu_percent(interval=0.3)
    cpu_per_core = psutil.cpu_percent(interval=0.2, percpu=True)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    load = os.getloadavg()
    io = _sample_io()
    return {
        "cpu": {
            "percent": cpu_percent,
            "cores": psutil.cpu_count(),
            "per_core": cpu_per_core,
            "load": {"1m": round(load[0], 2), "5m": round(load[1], 2), "15m": round(load[2], 2)},
            "temp": _try_cpu_temp(),
        },
        "memory": {
            "total": mem.total, "used": mem.used, "available": mem.available,
            "percent": round(mem.percent, 1),
        },
        "disk": {
            "total": disk.total, "used": disk.used, "free": disk.free,
            "percent": round(disk.percent, 1),
        },
        "io": io,
        "uptime": int(time.time() - psutil.boot_time()),
        "timestamp": int(time.time()),
    }


def _try_cpu_temp() -> float | None:
    try:
        temps = psutil.sensors_temperatures()
        for name, entries in temps.items():
            if entries:
                return round(entries[0].current, 1)
    except Exception:
        pass
    return None


def _nginx_active() -> bool:
    """Check if nginx is running by detecting listening ports."""
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = proc.info["name"] or ""
            cmdline = proc.info["cmdline"] or []
            if name == "nginx" or any("nginx" in str(c) for c in cmdline):
                for conn in psutil.net_connections(kind="inet"):
                    if conn.status == "LISTEN" and conn.laddr.port in (80, 443):
                        return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


async def collect_services() -> dict:
    """Check service statuses concurrently."""
    env = get_user_env()
    loop = asyncio.get_event_loop()
    tasks = {
        "gateway": _service_active(["systemctl", "--user", "is-active", "hermes-gateway"], env),
        "hermes": _hermes_ok(env),
        "nginx": loop.run_in_executor(None, _nginx_active),
    }
    results = {}
    for name, coro in tasks.items():
        try:
            results[name] = await coro
        except Exception:
            results[name] = False
    return results


async def _service_active(cmd, env=None):
    rc, out, _ = await arun(cmd, env=env or os.environ)
    return out.strip() == "active"


async def _hermes_ok(env):
    rc, _, _ = await arun([cfg.HERMES_BIN, "status"], env=env, timeout=10)
    return rc == 0


async def collect_monthly_traffic() -> dict | None:
    """Get monthly traffic from OCI Monitoring API."""
    return await _get_oci_network_usage()


_oci_net_cache = {"data": None, "ts": 0.0}
_OCI_NET_TTL = 3600  # 1 hour — monthly traffic changes slowly


async def _get_oci_network_usage() -> dict | None:
    """Query OCI Monitoring API for monthly VCN traffic (all VNICs aggregated)."""
    global _oci_net_cache
    now = time.time()
    if not cfg.OCI_ENABLED:
        return None

    # Return cached if fresh
    if _oci_net_cache["data"] and now - _oci_net_cache["ts"] < _OCI_NET_TTL:
        return _oci_net_cache["data"]

    try:
        import oci as oci_sdk
        from datetime import datetime, timezone

        config = oci_sdk.config.from_file(cfg.OCI_CONFIG)
        tenancy = config["tenancy"]
        monitoring = oci_sdk.monitoring.MonitoringClient(config)

        utc_now = datetime.now(timezone.utc)
        month_start = utc_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        rx_total = 0.0
        tx_total = 0.0

        for metric_name, is_tx in [("VnicToNetworkBytes", True), ("VnicFromNetworkBytes", False)]:
            query = f"{metric_name}[1d].sum()"
            resp = monitoring.summarize_metrics_data(
                compartment_id=tenancy,
                summarize_metrics_data_details=oci_sdk.monitoring.models.SummarizeMetricsDataDetails(
                    namespace="oci_vcn",
                    query=query,
                    start_time=month_start,
                    end_time=utc_now,
                    resolution="1d",
                ),
            )
            for item in resp.data:
                for dp in item.aggregated_datapoints:
                    if is_tx:
                        tx_total += dp.value
                    else:
                        rx_total += dp.value

        # Convert to integer bytes (API returns floats)
        result = {
            "year": utc_now.year,
            "month": utc_now.month,
            "rx": int(rx_total),
            "tx": int(tx_total),
            "source": "oci_vcn",
        }
        _oci_net_cache = {"data": result, "ts": now}
        return result
    except Exception as e:
        # Stale cache better than nothing
        if _oci_net_cache["data"]:
            return _oci_net_cache["data"]
        return None


_SNAPSHOT_PATH = Path(os.path.expanduser("~/.openviking/data/.daily_snapshot.json"))


def _load_snapshot() -> dict:
    """Load the daily snapshot; returns {date, writes, queries} or None."""
    try:
        if _SNAPSHOT_PATH.exists():
            return json.loads(_SNAPSHOT_PATH.read_text())
    except Exception:
        pass
    return None


def _save_snapshot(data: dict):
    """Persist the daily snapshot."""
    try:
        _SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SNAPSHOT_PATH.write_text(json.dumps(data))
    except Exception:
        pass


async def collect_memory_engine() -> dict:
    """Get memory engine stats from OpenViking, daily-reset via snapshot.

    total        — vector count (indexed memory chunks)
    today_writes — viking_remember tool calls since midnight (from plugin counter)
    today_reads  — viking_search + viking_read since midnight (from retrieval observer)
    """
    ov_url = "http://127.0.0.1:1933"
    today = datetime.now().strftime("%Y-%m-%d")
    result = {"today_writes": 0, "today_reads": 0, "total": 0}

    # 1. Total — vector count from vikingdb observer
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{ov_url}/api/v1/observer/vikingdb")
            if r.status_code == 200:
                table = r.json().get("result", {}).get("status", "")
                for line in table.split("\n"):
                    cells = [c.strip() for c in line.split("|") if c.strip()]
                    if len(cells) >= 3 and cells[0] == "TOTAL":
                        result["total"] = int(cells[2])
    except Exception:
        pass

    # 2. Raw writes — from OpenViking plugin tool counter
    raw_writes = 0
    try:
        stats_path = Path(os.path.expanduser("~/.openviking/data/.tool_stats.json"))
        if stats_path.exists():
            stats = json.loads(stats_path.read_text())
            raw_writes = stats.get("writes", 0)
        else:
            # seed zeros so the file always exists
            stats_path.parent.mkdir(parents=True, exist_ok=True)
            stats_path.write_text(json.dumps({"writes": 0, "queries": 0, "resources": 0}))
    except Exception:
        pass

    # 3. Raw queries — from observer/retrieval (Total Queries)
    raw_queries = 0
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{ov_url}/api/v1/observer/retrieval")
            if r.status_code == 200:
                table = r.json().get("result", {}).get("status", "")
                for line in table.split("\n"):
                    cells = [cell.strip() for cell in line.split("|") if cell.strip()]
                    if len(cells) >= 2 and cells[0] == "Total Queries":
                        raw_queries = int(cells[1])
    except Exception:
        pass

    # 4. Daily snapshot: compute today = raw - snapshot
    snap = _load_snapshot()
    if snap is None or snap.get("date") != today:
        # New day (or first run) → reset snapshot to current raw values
        snap = {"date": today, "writes": raw_writes, "queries": raw_queries}
        _save_snapshot(snap)
        result["today_writes"] = 0
        result["today_reads"] = 0
    else:
        # Same day — delta from snapshot
        # Handle counter reset (process restart): if raw < snap, treat as fresh start
        if raw_writes >= snap["writes"]:
            result["today_writes"] = raw_writes - snap["writes"]
        else:
            snap["writes"] = raw_writes
            _save_snapshot(snap)

        if raw_queries >= snap["queries"]:
            result["today_reads"] = raw_queries - snap["queries"]
        else:
            snap["queries"] = raw_queries
            _save_snapshot(snap)

    return result


async def collect_processes() -> dict:
    """Get top CPU and memory processes."""
    psutil.cpu_percent(interval=None)  # prime counters
    procs = []
    for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "memory_info"]):
        try:
            info = proc.info
            mem_mb = round(info["memory_info"].rss / 1048576, 1) if info["memory_info"] else 0
            procs.append({
                "pid": info["pid"], "name": info["name"],
                "cpu": round(info["cpu_percent"] or 0, 1),
                "mem_mb": mem_mb,
                "mem_percent": round(info["memory_percent"] or 0, 1),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return {
        "top_cpu": sorted(procs, key=lambda p: p["cpu"], reverse=True)[:5],
        "top_mem": sorted(procs, key=lambda p: p["mem_mb"], reverse=True)[:5],
    }


# ── REST Endpoints ─────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "2.0.1", "uptime": int(time.time() - psutil.boot_time())}


@app.get("/api/system")
async def system_stats():
    return collect_system()


@app.get("/api/services")
async def services_status():
    return await collect_services()


@app.get("/api/processes")
async def process_list():
    return await collect_processes()


@app.get("/api/network")
async def network_stats():
    n = psutil.net_io_counters()
    monthly = await collect_monthly_traffic()
    io = _sample_io()
    return {
        "speed_up": io["net_up_bps"],
        "speed_down": io["net_down_bps"],
        "total_sent": n.bytes_sent,
        "total_recv": n.bytes_recv,
        "monthly": monthly,
        "boot": {"rx": n.bytes_recv, "tx": n.bytes_sent, "uptime": int(time.time() - psutil.boot_time())},
    }


# ── OCI Cost (cached) ──────────────────────────────────────────────

_GLOBAL_OCI_CACHE = {"data": None, "ts": 0.0, "prev_total": None}
_OCI_TTL = 3600  # 1 hour — OCI billing data updates infrequently

@app.get("/api/oci/cost")
async def oci_cost():
    """Get OCI cost summary for current month (cached 1h)."""
    global _GLOBAL_OCI_CACHE
    now = time.time()
    if not cfg.OCI_ENABLED:
        return {"enabled": False, "error": "OCI not configured"}

    # Return cached data if still fresh
    if _GLOBAL_OCI_CACHE["data"] and now - _GLOBAL_OCI_CACHE["ts"] < _OCI_TTL:
        return _GLOBAL_OCI_CACHE["data"]

    try:
        import oci as oci_sdk
        from datetime import datetime, timezone

        oci_config = oci_sdk.config.from_file(cfg.OCI_CONFIG)
        tenancy = oci_config["tenancy"]
        client = oci_sdk.usage_api.UsageapiClient(oci_config)

        today = datetime.now(timezone.utc)
        start = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = today.replace(hour=0, minute=0, second=0, microsecond=0)

        query = oci_sdk.usage_api.models.RequestSummarizedUsagesDetails(
            tenant_id=tenancy,
            time_usage_started=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            time_usage_ended=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            granularity="MONTHLY",
            group_by=["service"],
        )

        result = client.request_summarized_usages(query)
        items = result.data.items
        services = []
        total = 0.0
        currency = "USD"
        for item in items:
            amt = round(item.computed_amount, 4)
            total += item.computed_amount
            if item.currency:
                currency = item.currency
            services.append({
                "service": item.service or "Unknown",
                "amount": amt,
            })

        cur_total = round(total, 2)
        prev = _GLOBAL_OCI_CACHE.get("prev_total")
        delta = round(cur_total - prev, 2) if prev is not None else 0.0
        data = {
            "enabled": True,
            "period": start.strftime("%Y-%m"),
            "currency": currency,
            "total": cur_total,
            "delta": delta,
            "services": services,
        }
        _GLOBAL_OCI_CACHE.update({"data": data, "ts": now, "prev_total": cur_total})
        return data
    except Exception as e:
        # Return stale cache if available, otherwise show short error
        if _GLOBAL_OCI_CACHE["data"]:
            _GLOBAL_OCI_CACHE["data"]["cached"] = True
            return _GLOBAL_OCI_CACHE["data"]
        return {"enabled": True, "error": "OCI 费用获取失败，稍后重试"}


# ── Hermes Endpoints ───────────────────────────────────────────────

@app.get("/api/hermes/model")
async def hermes_model():
    try:
        rc, out, _ = await arun([cfg.HERMES_BIN, "config"], env=get_user_env(), timeout=10)
        model = provider = "unknown"
        m = re.search(r"'default':\s*'([^']*)'", out)
        if m:
            model = m.group(1)
        p = re.search(r"'provider':\s*'([^']*)'", out)
        if p:
            provider = p.group(1)
        return {"model": model, "provider": provider}
    except Exception as e:
        return {"model": "error", "provider": str(e)}


@app.get("/api/hermes/platforms")
async def hermes_platforms():
    platforms = {}
    # Parse from hermes config output
    try:
        rc, out, _ = await arun([cfg.HERMES_BIN, "config"], env=get_user_env(), timeout=10)
        for name, pattern in [("Telegram", r"Telegram:\s*(\S+)"), ("Discord", r"Discord:\s*(\S+)")]:
            m = re.search(pattern, out)
            if m:
                platforms[name] = m.group(1) == "configured"
    except Exception:
        pass
    # Check QQ from config.yaml
    config_path = Path(cfg.HERMES_HOME) / "config.yaml"
    if config_path.exists():
        content = config_path.read_text()
        platforms["QQ"] = "qqbot:" in content and "enabled: true" in content
    # Check 微信 from .env
    env_path = Path(cfg.HERMES_HOME) / ".env"
    if env_path.exists():
        platforms["微信"] = "WEIXIN_TOKEN=" in env_path.read_text()

    # Filter to known platforms only
    result = {k: v for k, v in platforms.items() if k in ("Telegram", "QQ", "微信")}
    return {"platforms": result}


@app.get("/api/hermes/memory")
async def hermes_memory():
    """Get memory stats from OpenViking API."""
    data = await collect_memory_engine()
    return {"count": data["total"], "today_writes": data["today_writes"], "today_queries": data["today_reads"]}


@app.get("/api/hermes/engines")
async def hermes_engines():
    """Get all 4 engine statuses: LLM, Memory, Vector, Local Retrieval."""
    result = {
        "llm": {"name": "未知", "provider": "未知"},
        "memory": {"provider": "未知", "status": "unknown"},
        "vector": {"provider": "未知", "model": "未知", "dimension": "未知", "active": False},
        "retrieval": {"provider": "未知", "model": "未知", "active": False},
    }

    # 1. LLM Engine
    try:
        rc, out, _ = await arun([cfg.HERMES_BIN, "config"], env=get_user_env(), timeout=10)
        m = re.search(r"'default':\s*'([^']*)'", out)
        p = re.search(r"'provider':\s*'([^']*)'", out)
        if m: result["llm"]["name"] = m.group(1)
        if p: result["llm"]["provider"] = p.group(1)
    except Exception:
        pass

    # 2. Memory Engine (OpenViking)
    try:
        rc, out, _ = await arun([cfg.HERMES_BIN, "memory"], env=get_user_env(), timeout=10)
        for line in out.splitlines():
            if "Status:" in line: result["memory"]["status"] = line.split("Status:")[1].strip()
            if "Provider:" in line: result["memory"]["provider"] = line.split("Provider:")[1].strip()
    except Exception:
        pass

    # 3. Vector Engine & 4. Local Retrieval - from OpenViking config
    ov_conf = Path("/home/ubuntu/.openviking/ov.conf")
    if ov_conf.exists():
        try:
            ov_data = json.loads(ov_conf.read_text())
            emb = ov_data.get("embedding", {}).get("dense", {})
            vlm = ov_data.get("vlm", {})

            # Vector engine — uses OpenViking local embedding (not Ollama)
            result["vector"] = {
                "provider": f"本地 ({emb.get('provider','?')})",
                "model": emb.get("model", "未知"),
                "dimension": emb.get("dimension", "未知"),
                "active": True,
            }

            # Local retrieval engine (VLM) — via LiteLLM
            result["retrieval"] = {
                "provider": f"LiteLLM ({vlm.get('provider','?')})",
                "model": vlm.get("model", "未知"),
                "active": True,
            }
        except Exception:
            pass

    return result


_vlm_last_query_snapshot = {"queries": 0, "last_used": None}


@app.get("/api/hermes/local-models")
async def local_models_status():
    """Real-time status for two local models: embedding & VLM."""
    result = {
        "embedding": {
            "model": "未知", "provider": "local",
            "status": "offline", "calls": 0, "tokens": 0,
            "last_used": None, "queue_pending": 0, "queue_active": 0,
            "avg_latency_ms": 0,
        },
        "vlm": {
            "model": "未知", "provider": "ollama",
            "status": "offline", "loaded": False, "processor": "CPU",
            "param_size": "", "quant": "",
            "queries": 0, "avg_latency_ms": 0,
            "zero_result_rate": 0, "last_used": None,
            "queue_pending": 0, "queue_active": 0,
        },
    }

    ov_url = "http://127.0.0.1:1933"

    # 1. Embedding model from OpenViking observer/models
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{ov_url}/api/v1/observer/models")
            if r.status_code == 200:
                data = r.json().get("result", {})
                raw = data.get("status", "")
                # Parse table: bge-small-zh-v1.5-f16 | local | 53 | 6471 | 0 | 6471 | timestamp
                import re
                lines = raw.strip().splitlines()
                for line in lines:
                    if line.startswith("|") and "Model" not in line and "---+---" not in line and line.count("|") >= 6:
                        parts = [p.strip() for p in line.split("|") if p.strip()]
                        if len(parts) >= 6:
                            result["embedding"]["model"] = parts[0]
                            result["embedding"]["provider"] = parts[1]
                            try:
                                result["embedding"]["calls"] = int(parts[2])
                                result["embedding"]["tokens"] = int(parts[5])
                            except ValueError:
                                pass
                            result["embedding"]["status"] = "active"
                            if len(parts) >= 7 and parts[6]:
                                result["embedding"]["last_used"] = parts[6]
    except Exception:
        pass

    # 2. Queue status for embedding
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{ov_url}/api/v1/observer/queue")
            if r.status_code == 200:
                data = r.json().get("result", {})
                raw = data.get("status", "")
                for line in raw.splitlines():
                    if "Embedding" in line:
                        parts = [p.strip() for p in line.split("|") if p.strip()]
                        if len(parts) >= 4:
                            result["embedding"]["queue_pending"] = int(parts[1])
                            result["embedding"]["queue_active"] = int(parts[2])
                        break
                # Update status based on queue
                emb = result["embedding"]
                if emb["status"] == "active":
                    if emb["queue_active"] > 0:
                        emb["status"] = "processing"
                    elif emb["queue_pending"] > 0:
                        emb["status"] = "queued"
                    else:
                        emb["status"] = "idle"
    except Exception:
        pass

    # 3. VLM model info — read from ov.conf (local or remote provider)
    # Ollama is no longer used; VLM now uses remote APIs via LiteLLM
    try:
        ov_conf = Path("/home/ubuntu/.openviking/ov.conf")
        if ov_conf.exists():
            ov_data = json.loads(ov_conf.read_text())
            vlm = ov_data.get("vlm", {})
            if vlm.get("model"):
                result["vlm"]["model"] = vlm["model"]
            if vlm.get("provider"):
                result["vlm"]["provider"] = vlm["provider"]
            # Remote API models and local Ollama models are "online" when provider is set
            if vlm.get("provider") in ("litellm", "openai", "deepseek", "ollama"):
                result["vlm"]["status"] = "online"
                result["vlm"]["loaded"] = True
    except Exception:
        pass

    # 4. Retrieval stats from OpenViking observer/retrieval
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{ov_url}/api/v1/observer/retrieval")
            if r.status_code == 200:
                data = r.json().get("result", {})
                raw = data.get("status", "")
                import re
                # Parse table metrics
                m_q = re.search(r"Total Queries\s+\|\s*(\d+)", raw)
                if m_q: result["vlm"]["queries"] = int(m_q.group(1))
                m_l = re.search(r"Avg Latency \(ms\)\s+\|\s*([\d.]+)", raw)
                if m_l: result["vlm"]["avg_latency_ms"] = round(float(m_l.group(1)), 1)
                m_z = re.search(r"Zero-Result Rate\s+\|\s*([\d.]+)%", raw)
                if m_z: result["vlm"]["zero_result_rate"] = round(float(m_z.group(1)), 1)
    except Exception:
        pass

    # Track last query time — detect queries increase
    global _vlm_last_query_snapshot
    cur_q = result["vlm"]["queries"]
    if cur_q > _vlm_last_query_snapshot["queries"]:
        _vlm_last_query_snapshot["queries"] = cur_q
        _vlm_last_query_snapshot["last_used"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    result["vlm"]["last_used"] = _vlm_last_query_snapshot["last_used"]

    # 5. Queue for semantic/VLM tasks
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{ov_url}/api/v1/observer/queue")
            if r.status_code == 200:
                data = r.json().get("result", {})
                raw = data.get("status", "")
                for line in raw.splitlines():
                    if "Semantic" in line and "Nodes" not in line:
                        parts = [p.strip() for p in line.split("|") if p.strip()]
                        if len(parts) >= 4:
                            result["vlm"]["queue_pending"] = int(parts[1])
                            result["vlm"]["queue_active"] = int(parts[2])
                        break
    except Exception:
        pass

    # Fallback status from ov.conf
    if result["embedding"]["model"] == "未知":
        try:
            ov_conf = Path("/home/ubuntu/.openviking/ov.conf")
            if ov_conf.exists():
                ov_data = json.loads(ov_conf.read_text())
                emb = ov_data.get("embedding", {}).get("dense", {})
                result["embedding"]["model"] = emb.get("model", "未知")
                result["embedding"]["provider"] = emb.get("provider", "local")
        except Exception:
            pass

    return result


@app.get("/api/hermes/overview")
async def hermes_overview():
    """Aggregate all Hermes tab data in one call (model + platforms + memory + engines + local-models)."""
    data = {"model": {}, "platforms": {}, "memory": {}, "engines": {}, "local_models": {}}

    # Read config.yaml once for model + platforms
    config_path = Path(cfg.HERMES_HOME) / "config.yaml"
    config_text = config_path.read_text() if config_path.exists() else ""

    # 1. Model
    try:
        rc, out, _ = await arun([cfg.HERMES_BIN, "config"], env=get_user_env(), timeout=10)
        model = provider = "unknown"
        m = re.search(r"'default':\s*'([^']*)'", out)
        if m: model = m.group(1)
        p = re.search(r"'provider':\s*'([^']*)'", out)
        if p: provider = p.group(1)
        data["model"] = {"model": model, "provider": provider}
    except Exception:
        pass

    # 2. Platforms
    platforms = {}
    try:
        rc, out, _ = await arun([cfg.HERMES_BIN, "config"], env=get_user_env(), timeout=10)
        for name, pattern in [("Telegram", r"Telegram:\s*(\S+)"), ("Discord", r"Discord:\s*(\S+)")]:
            m = re.search(pattern, out)
            if m: platforms[name] = m.group(1) == "configured"
    except Exception:
        pass
    if config_text:
        platforms["QQ"] = "qqbot:" in config_text and "enabled: true" in config_text
    env_path = Path(cfg.HERMES_HOME) / ".env"
    if env_path.exists():
        platforms["微信"] = "WEIXIN_TOKEN=" in env_path.read_text()
    data["platforms"] = {k: v for k, v in platforms.items() if k in ("Telegram", "QQ", "微信")}

    # 3. Memory (OpenViking)
    mem = await collect_memory_engine()
    data["memory"] = {"count": mem["total"], "today_writes": mem["today_writes"], "today_queries": mem["today_reads"]}

    # 4. Engines (LLM + Memory Engine only — vector & retrieval moved to local_models)
    engines = {"llm": {"name": "未知", "provider": "未知"}, "memory": {"provider": "未知", "status": "unknown"}}
    try:
        rc, out, _ = await arun([cfg.HERMES_BIN, "config"], env=get_user_env(), timeout=10)
        m = re.search(r"'default':\s*'([^']*)'", out)
        p = re.search(r"'provider':\s*'([^']*)'", out)
        if m: engines["llm"]["name"] = m.group(1)
        if p: engines["llm"]["provider"] = p.group(1)
    except Exception:
        pass
    try:
        rc, out, _ = await arun([cfg.HERMES_BIN, "memory"], env=get_user_env(), timeout=10)
        for line in out.splitlines():
            if "Status:" in line: engines["memory"]["status"] = line.split("Status:")[1].strip()
            if "Provider:" in line: engines["memory"]["provider"] = line.split("Provider:")[1].strip()
    except Exception:
        pass
    data["engines"] = engines

    # 5. Local models (reuse existing endpoint logic)
    lm = await local_models_status()
    data["local_models"] = lm

    return data


@app.get("/api/hermes/alerts")
async def hermes_alerts():
    """Last 24h journalctl errors."""
    try:
        rc, out, _ = await arun(
            ["journalctl", "--user", "-u", "hermes-gateway", "--since", "24 hours ago", "--no-pager"],
            env=get_user_env(), timeout=10,
        )
        alerts = [l.strip() for l in out.splitlines() if "ERROR" in l or "WARNING" in l or "CRITICAL" in l]
        return {"alerts": alerts[-20:], "total": len(alerts)}
    except Exception as e:
        return {"alerts": [], "error": str(e)}


# ── Cron Jobs Endpoint ─────────────────────────────────────────────

@app.get("/api/cron/jobs")
async def cron_jobs():
    """List all scheduled cron jobs with their last run status."""
    rc, out, _ = await arun([cfg.HERMES_BIN, "cron", "list"], env=get_user_env(), timeout=15)
    jobs = []
    if rc == 0:
        current_job = {}
        for line in out.splitlines():
            line = line.rstrip()
            # Job ID line: "  ec53307311cf [active]"
            m = re.match(r"^\s{2}([0-9a-f]{12})\s+\[(\w+)\]", line)
            if m:
                if current_job:
                    jobs.append(current_job)
                current_job = {"id": m.group(1), "state": m.group(2)}
                continue
            # Field lines: "    Name:      QQ邮箱检查"
            if current_job:
                m2 = re.match(r"^\s+([\w ]+):\s+(.*)$", line)
                if m2:
                    key = m2.group(1).lower()
                    val = m2.group(2).strip()
                    if key == "name":
                        current_job["name"] = val
                    elif key == "last run":
                        # "Last run:  ... ok" or "... error: RuntimeError: ..."
                        if "error:" in val:
                            current_job["last_run"] = val.split("error:")[0].strip()
                            current_job["status"] = "error"
                        else:
                            parts = val.rsplit(" ", 1)
                            current_job["last_run"] = parts[0].strip()
                            current_job["status"] = parts[1] if len(parts) > 1 else "unknown"
        if current_job:
            jobs.append(current_job)
    return {"jobs": jobs}


# ── Ops / Command Endpoints ────────────────────────────────────────

@app.get("/api/ops/check-access")
async def ops_check_access(request: Request):
    ip = get_client_ip(request)
    allowed = ip in cfg.OPS_WHITELIST
    return {"ip": ip, "allowed": allowed, "need_password": not allowed}


@app.post("/api/ops/verify-password")
async def ops_verify_password(request: Request):
    data = await request.json()
    ip = get_client_ip(request)
    pw = data.get("password", "")
    if pw and pw == cfg.OPS_PASSWORD:
        return {"success": True, "ip": ip}
    raise HTTPException(403, f"Wrong password for {ip}")


@app.post("/api/ops/emergency-restart")
async def ops_emergency_restart(request: Request):
    check_ops(request)
    check_auth(request)
    env = get_user_env()
    results = []

    # 1. Stop gateway
    rc, _, _ = await arun(["systemctl", "--user", "stop", "hermes-gateway"], env=env)
    results.append(f"Stop gateway → exit={rc}")

    # 2. Kill all hermes processes
    rc, out, _ = await arun(["pgrep", "-f", "hermes"], timeout=5)
    killed = []
    for pid in out.strip().splitlines():
        pid = pid.strip()
        if pid and pid != str(os.getpid()):
            try:
                os.kill(int(pid), signal.SIGKILL)
                killed.append(pid)
            except Exception:
                pass
    results.append(f"Killed PIDs: {', '.join(killed) if killed else 'none'}")

    await asyncio.sleep(2)

    # 3. Start gateway
    rc, _, _ = await arun(["systemctl", "--user", "start", "hermes-gateway"], env=env)
    results.append(f"Start gateway → exit={rc}")

    await asyncio.sleep(1)

    # 4. Verify
    active = await _service_active(["systemctl", "--user", "is-active", "hermes-gateway"], env)
    results.append(f"Gateway: {'✅ running' if active else '❌ not running'}")

    return {"results": results}


@app.post("/api/ops/restart-service")
async def ops_restart_service(request: Request):
    check_ops(request)
    check_auth(request)
    data = await request.json()
    service = data.get("service", "")
    if service not in cfg.SERVICES:
        raise HTTPException(400, f"Unknown service: {service}")
    cmd, is_user = cfg.SERVICES[service]
    env = get_user_env() if is_user else None
    rc, out, err = await arun(cmd, env=env or os.environ, timeout=15)
    return {"service": service, "exit_code": rc}


# ── Blog Widget (external embed) ───────────────────────────────────

@app.get("/api/blog-widget")
async def blog_widget():
    """Return data in the format expected by the blog embed widget."""
    sys_data = collect_system()
    mem_eng = await collect_memory_engine()
    monthly = await collect_monthly_traffic()
    io = _sample_io()

    return {
        "cpu": {
            "cores": sys_data["cpu"]["per_core"],
            "percent": sys_data["cpu"]["percent"],
            "load": sys_data["cpu"]["load"],
        },
        "memory": {
            "percent": sys_data["memory"]["percent"],
            "used_gb": round(sys_data["memory"]["used"] / 1073741824, 1),
            "total_gb": round(sys_data["memory"]["total"] / 1073741824, 0),
        },
        "disk": {
            "percent": sys_data["disk"]["percent"],
            "used": sys_data["disk"]["used"],
            "total": sys_data["disk"]["total"],
        },
        "net": {
            "download_per_sec": io["net_down_bps"],
        },
        "monthly_traffic": monthly,
        "uptime": sys_data["uptime"],
        "memory_engine": mem_eng,
    }


# ── SSE Real-time Stream ───────────────────────────────────────────

@app.get("/api/stream")
async def stream_metrics(request: Request):
    """SSE endpoint — pushes system/network/services/processes every N seconds."""

    async def event_generator():
        interval = float(request.query_params.get("interval", "5"))
        while True:
            try:
                sys_data = collect_system()
                svc_data = await collect_services()
                proc_data = await collect_processes()
                net_data = await network_stats()
                oci_data = await oci_cost()
                payload = {
                    "system": sys_data,
                    "services": svc_data,
                    "processes": proc_data,
                    "network": net_data,
                    "oci": oci_data,
                    "timestamp": int(time.time()),
                }
                yield {"event": "metrics", "data": json.dumps(payload, ensure_ascii=False, default=str)}
            except Exception as e:
                yield {"event": "error", "data": str(e)}
            await asyncio.sleep(interval)

    return EventSourceResponse(event_generator())


# ── Chat: llama-server (MiniCPM-V-4_5) ───────────────────────────────

LLAMA_CHAT_URL = "http://127.0.0.1:8081/v1/chat/completions"


@app.post("/api/chat")
async def chat(request: Request):
    """POST {message} → SSE stream. Only streams 'content' tokens; falls back to reasoning_content if content is empty."""
    body = await request.json()
    prompt = body.get("message", "").strip()
    if not prompt:
        raise HTTPException(400, "message is required")

    async def token_stream():
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=300, write=10, pool=10)) as client:
                collected_reasoning = []
                has_content = False

                async with client.stream("POST", LLAMA_CHAT_URL, json={
                    "model": "Qwen3.6-35B-A3B-Q4_K_M.gguf",
                    "messages": [
                        {"role": "system", "content": "你是一个简洁高效、严肃认真、逻辑严谨的AI助手。回答直接了当，不废话。"},
                        {"role": "user", "content": prompt}
                    ],
                    "max_tokens": 512,
                    "thinking": {"type": "none"},
                    "stream": True,
                }) as resp:
                    if resp.status_code != 200:
                        error_body = await resp.aread()
                        yield {"event": "error", "data": f"llama-server {resp.status_code}: {error_body.decode()[:200]}"}
                        return

                    async for raw_line in resp.aiter_lines():
                        if not raw_line.startswith("data: "):
                            continue
                        payload = raw_line[6:].strip()
                        if payload == "[DONE]":
                            # If no content was ever streamed, send reasoning as fallback
                            if not has_content and collected_reasoning:
                                reasoning = "".join(collected_reasoning).strip()
                                if reasoning:
                                    yield {"event": "token", "data": reasoning}
                            yield {"event": "done", "data": ""}
                            return
                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        content_tok = delta.get("content") or ""
                        reasoning_tok = delta.get("reasoning_content") or ""

                        if content_tok:
                            has_content = True
                            yield {"event": "token", "data": content_tok}
                        if reasoning_tok:
                            collected_reasoning.append(reasoning_tok)

                    # Stream ended without [DONE] — same fallback
                    if not has_content and collected_reasoning:
                        reasoning = "".join(collected_reasoning).strip()
                        if reasoning:
                            yield {"event": "token", "data": reasoning}
                    yield {"event": "done", "data": ""}
        except Exception as e:
            yield {"event": "error", "data": str(e)}

    return EventSourceResponse(token_stream())


# ── Entry ──────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(static_dir / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host=cfg.SERVER_HOST, port=cfg.SERVER_PORT, log_level="info")
