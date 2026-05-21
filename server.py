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
from fastapi.responses import FileResponse, JSONResponse, Response
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
        "gateway": (["sudo", "systemctl", "restart", "hermes-gateway"], False),
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


# ── Favicon ────────────────────────────────────────────────────────
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Serve favicon from static directory."""
    favicon_path = static_dir / "favicon.ico"
    if favicon_path.is_file():
        return FileResponse(
            favicon_path,
            media_type="image/x-icon",
            headers={"Cache-Control": "public, max-age=86400"}
        )
    raise HTTPException(status_code=404)


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
    cpu_percent = psutil.cpu_percent(interval=0)
    cpu_per_core = psutil.cpu_percent(interval=0, percpu=True)
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
        "gateway": _service_active(["systemctl", "is-active", "hermes-gateway"], env),
        "hermes": _hermes_ok(env),
        "nginx": loop.run_in_executor(None, _nginx_active),
        "llamacpp": _service_active(["systemctl", "is-active", "llama-server"], env),
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


_TDAI_SNAPSHOT_PATH = Path(os.path.expanduser("~/.memory-tencentdb/.daily_snapshot.json"))


def _load_tdai_snapshot() -> dict:
    """Load the daily snapshot for TencentDB; returns {date, writes, reads} or None."""
    try:
        if _TDAI_SNAPSHOT_PATH.exists():
            return json.loads(_TDAI_SNAPSHOT_PATH.read_text())
    except Exception:
        pass
    return None


def _save_tdai_snapshot(data: dict):
    """Persist the daily snapshot for TencentDB."""
    try:
        _TDAI_SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _TDAI_SNAPSHOT_PATH.write_text(json.dumps(data))
    except Exception:
        pass


async def collect_tdai_memory_engine() -> dict:
    """Get memory engine stats from TencentDB Gateway + SQLite.

    total        — L1 memory records count
    today_writes — L0 captures + L1 extractions today
    today_reads  — Recall completed count from syslog today (CST)
    week_recalls — Recall completed count from syslog this week
    """
    gateway_url = "http://127.0.0.1:8420"
    db_path = Path.home() / ".memory-tencentdb/memory-tdai/vectors.db"
    today = datetime.now().strftime("%Y-%m-%d")  # Beijing time for user-facing display
    result = {"today_writes": 0, "today_reads": 0, "total": 0}

    # 1. Health check
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{gateway_url}/health")
            if r.status_code == 200:
                data = r.json()
                if not data.get("stores", {}).get("vectorStore"):
                    return result
    except Exception:
        return result

    # 2. SQLite stats (TencentDB schema)
    if not db_path.exists():
        return result

    try:
        import sqlite3
        import subprocess
        import json as _json_mod
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()

        # Total L1 records (structured memories)
        cur.execute("SELECT COUNT(*) FROM l1_records")
        result["total"] = cur.fetchone()[0]

        # Total L0 conversations (raw turns)
        cur.execute("SELECT COUNT(*) FROM l0_conversations")
        result["l0_count"] = cur.fetchone()[0]

        # Today's L1 writes (created_time is UTC ISO timestamp, add 8h for Beijing time)
        cur.execute(
            "SELECT COUNT(*) FROM l1_records WHERE DATE(created_time, '+8 hours') = ?",
            (today,)
        )
        l1_writes = cur.fetchone()[0]

        # Today's L0 writes (recorded_at is UTC ISO timestamp, add 8h for Beijing time)
        cur.execute(
            "SELECT COUNT(*) FROM l0_conversations WHERE DATE(recorded_at, '+8 hours') = ?",
            (today,)
        )
        l0_writes = cur.fetchone()[0]

        # Combined: L0 captures + L1 extractions
        raw_writes = l0_writes + l1_writes

        # Today's reads: count "Recall completed" entries in syslog for today (CST)
        try:
            today_cst = datetime.now().strftime("%Y-%m-%d")
            cmd = f"grep 'Recall completed' /var/log/syslog | grep '{today_cst}' | wc -l"
            raw_reads = int(subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip() or 0)
        except Exception:
            raw_reads = 0

        # Week's reads: syslog total (syslog rotates weekly)
        try:
            cmd_total = "grep -c 'Recall completed' /var/log/syslog"
            week_reads = int(subprocess.run(cmd_total, shell=True, capture_output=True, text=True).stdout.strip() or 0)
        except Exception:
            week_reads = raw_reads
        
        result["week_recalls"] = week_reads

        conn.close()

        # Direct SQL filter already gives today's count, no snapshot needed
        result["today_writes"] = raw_writes
        result["today_reads"] = raw_reads

    except Exception:
        pass

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
    """Get memory stats from TencentDB Gateway."""
    data = await collect_tdai_memory_engine()
    return {
        "l1_count": data["total"],          # 结构化记忆数
        "l0_count": data.get("l0_count", 0), # 原始对话轮数
        "today_writes": data["today_writes"],
        "today_queries": data["today_reads"],
        "week_recalls": data.get("week_recalls", 0)  # 本周检索次数
    }


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

    # 2. Memory Engine (TencentDB)
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get("http://127.0.0.1:8420/health")
            if r.status_code == 200:
                data = r.json()
                if data.get("stores", {}).get("vectorStore"):
                    result["memory"]["status"] = "active"
                    result["memory"]["provider"] = "TencentDB"
                else:
                    result["memory"]["status"] = "degraded"
            else:
                result["memory"]["status"] = "offline"
    except Exception:
        result["memory"]["status"] = "offline"

    # 3. Vector Engine & 4. Local Retrieval — from TencentDB Gateway
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get("http://127.0.0.1:8420/health")
            if r.status_code == 200:
                data = r.json()
                # Vector engine status (embedding: gemini-embedding-2 via Gemini API)
                result["vector"] = {
                    "provider": "Gemini",
                    "model": "gemini-embedding-2",
                    "dimension": "3072",
                    "active": data.get("stores", {}).get("embeddingService", False),
                }
                # Retrieval status
                result["retrieval"] = {
                    "provider": "MiniMax",
                    "model": "MiniMax-M2.5",
                    "active": data.get("stores", {}).get("vectorStore", False),
                }
    except Exception:
        pass

    return result



@app.get("/api/hermes/local-models")
async def local_models_status():
    """Real-time status for memory backend models."""
    result = {
        "embedding": {
            "model": "gemini-embedding-2",
            "provider": "Gemini",
            "status": "offline",
            "dimension": "3072",
            "uptime_sec": None,
        },
        "vlm": {
            "model": "glm-5",
            "provider": "Tencent Coding",
            "status": "offline",
            "gateway_uptime": None,
            "gateway_version": None,
        },
    }

    # Check embedding status from Gateway health
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get("http://127.0.0.1:8420/health")
            if r.status_code == 200:
                data = r.json()
                if data.get("stores", {}).get("embeddingService"):
                    result["embedding"]["status"] = "active"
                    result["embedding"]["uptime_sec"] = data.get("uptime")
    except Exception:
        pass

    # Check TencentDB Gateway (:8420)
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get("http://127.0.0.1:8420/health")
            if r.status_code == 200:
                data = r.json()
                if data.get("stores", {}).get("vectorStore"):
                    result["vlm"]["status"] = "online"
                    result["vlm"]["gateway_uptime"] = data.get("uptime")
                    result["vlm"]["gateway_version"] = data.get("version")
    except Exception:
        pass

    # Get memory count from SQLite
    try:
        import sqlite3
        db_path = os.path.expanduser("~/.memory-tencentdb/memory-tdai/vectors.db")
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM l1_records")
        result["vlm"]["memory_count"] = cur.fetchone()[0]
        conn.close()
    except Exception:
        pass

    # Get memory stats from syslog (syslog rotates weekly)
    try:
        import subprocess
        from datetime import datetime
        # Today's recalls
        today_cst = datetime.now().strftime("%Y-%m-%d")
        cmd_today = f"grep 'Recall completed' /var/log/syslog | grep '{today_cst}' | wc -l"
        today_queries = int(subprocess.run(cmd_today, shell=True, capture_output=True, text=True).stdout.strip() or 0)
        # Week's recalls (syslog total)
        cmd_week = "grep -c 'Recall completed' /var/log/syslog"
        week_queries = int(subprocess.run(cmd_week, shell=True, capture_output=True, text=True).stdout.strip() or 0)
        result["vlm"]["today_queries"] = today_queries
        result["vlm"]["total_queries"] = week_queries
    except Exception:
        result["vlm"]["today_queries"] = 0
        result["vlm"]["total_queries"] = 0
    
    # Get memory record count from SQLite
    try:
        import sqlite3
        db_path = Path.home() / ".memory-tencentdb" / "memory-tdai" / "vectors.db"
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()
            result["vlm"]["memory_count"] = cur.execute("SELECT COUNT(*) FROM l1_records").fetchone()[0]
            result["vlm"]["l0_count"] = cur.execute("SELECT COUNT(*) FROM l0_conversations").fetchone()[0]
            conn.close()
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

    # 3. Memory (TencentDB)
    mem = await collect_tdai_memory_engine()
    data["memory"] = {"count": mem["total"], "today_writes": mem["today_writes"], "today_queries": mem["today_reads"]}

    # 4. Engines (all 4 — unified status badges)
    engines = {
        "llm": {"name": "未知", "provider": "未知", "status": "offline"},
        "memory": {"provider": "未知", "status": "offline"},
    }
    # LLM
    try:
        rc, out, _ = await arun([cfg.HERMES_BIN, "config"], env=get_user_env(), timeout=10)
        m = re.search(r"'default':\s*'([^']*)'", out)
        p = re.search(r"'provider':\s*'([^']*)'", out)
        if m: engines["llm"]["name"] = m.group(1)
        if p: engines["llm"]["provider"] = p.group(1)
        if engines["llm"]["name"] != "未知":
            engines["llm"]["status"] = "active"
    except Exception:
        pass
    # Memory (TencentDB health)
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get("http://127.0.0.1:8420/health")
            if r.status_code == 200:
                hdata = r.json()
                emb_ok = hdata.get("stores", {}).get("embeddingService", False)
                vec_ok = hdata.get("stores", {}).get("vectorStore", False)
                engines["memory"]["provider"] = "TencentDB"
                if emb_ok and vec_ok:
                    engines["memory"]["status"] = "healthy"
                elif vec_ok:
                    engines["memory"]["status"] = "degraded"
                else:
                    engines["memory"]["status"] = "offline"
                engines["memory"]["uptime_sec"] = hdata.get("uptime")
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
            ["journalctl", "-u", "hermes-gateway", "--since", "24 hours ago", "--no-pager"],
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


# Init cache (30s TTL — balance speed vs freshness)
_INIT_CACHE = {"data": None, "ts": 0.0, "ip": None}
_INIT_TTL = 30


@app.get("/api/init")
async def init_data(request: Request):
    """Combined init endpoint — cached 30s at CDN + backend. Merges ops + hermes + cron."""
    global _INIT_CACHE
    ip = get_client_ip(request)
    now = time.time()

    # Return cached if fresh AND same IP (ops check is IP-specific)
    if _INIT_CACHE["data"] and now - _INIT_CACHE["ts"] < _INIT_TTL and _INIT_CACHE["ip"] == ip:
        return JSONResponse(content=_INIT_CACHE["data"], headers={"Cache-Control": "public, max-age=30"})

    # Parallel fetch all data
    ops_task = asyncio.create_task(ops_check_access(request))
    hermes_task = asyncio.create_task(hermes_overview())
    cron_task = asyncio.create_task(cron_jobs())
    ops, hermes, cron = await asyncio.gather(ops_task, hermes_task, cron_task)
    result = {"ops": ops, "hermes": hermes, "cron": cron}

    _INIT_CACHE = {"data": result, "ts": now, "ip": ip}
    return JSONResponse(content=result, headers={"Cache-Control": "public, max-age=30"})


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
    rc, _, _ = await arun(["sudo", "systemctl", "stop", "hermes-gateway"], env=env)
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
    rc, _, _ = await arun(["sudo", "systemctl", "start", "hermes-gateway"], env=env)
    results.append(f"Start gateway → exit={rc}")

    await asyncio.sleep(1)

    # 4. Verify
    active = await _service_active(["systemctl", "is-active", "hermes-gateway"], env)
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
    mem_eng = await collect_tdai_memory_engine()
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


# ── Blog Widget SSE Stream ───────────────────────────────────────────

@app.get("/api/blog-stream")
async def blog_stream(request: Request):
    """SSE endpoint for blog sidebar widget — pushes blog-widget format every N seconds."""

    async def event_generator():
        interval = float(request.query_params.get("interval", "5"))
        while True:
            if await request.is_disconnected():
                break
            try:
                sys_data = collect_system()
                mem_eng = await collect_tdai_memory_engine()
                monthly = await collect_monthly_traffic()
                io = _sample_io()
                payload = {
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
                yield {"event": "blog-metrics", "data": json.dumps(payload, ensure_ascii=False)}
            except Exception as e:
                yield {"event": "error", "data": str(e)}
            await asyncio.sleep(interval)

    return EventSourceResponse(event_generator())


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
    """Index.html with 1-year cache. Purge CDN on update."""
    return FileResponse(
        static_dir / "index.html",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host=cfg.SERVER_HOST, port=cfg.SERVER_PORT, log_level="info")
