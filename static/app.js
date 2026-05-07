/* ── Hermes Mini App v2 — Frontend ─────────────────────────────────── */
const API = '/api';
const TOKEN = 'hermes-mini-app-2024';
let opsPwd = '';
let opsAuth = false;

const TG = window.Telegram?.WebApp;
if (TG) { TG.expand(); TG.setHeaderColor('#0a0a14'); }

// ── Helpers ──────────────────────────────────────────────────────────

const $ = id => document.getElementById(id);
const esc = s => { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; };
const barColor = p => p > 85 ? 'b-red' : p > 60 ? 'b-yellow' : '';

function fmtBytes(bytes) {
  if (bytes === 0) return '0 B';
  const k = 1024, sizes = ['B','KB','MB','GB','TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k,i)).toFixed(1)) + ' ' + sizes[i];
}
function fmtSpeed(bps) { return fmtBytes(bps) + '/s'; }
function fmtUptime(s) {
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  if (d > 0) return d + 'd ' + h + 'h';
  const m = Math.floor((s % 3600) / 60);
  return (h > 0 ? h + 'h ' : '') + m + 'm';
}

// ── Tab Switching ────────────────────────────────────────────────────

function switchTab(tab) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  $(`page-${tab}`).classList.add('active');
  document.querySelector(`[data-tab="${tab}"]`).classList.add('active');
}

// ── Clock ────────────────────────────────────────────────────────────

function updateClock() {
  $('header-time').textContent = new Date().toLocaleTimeString('zh-CN');
}
setInterval(updateClock, 1000);
updateClock();

// ── Monitor Render ───────────────────────────────────────────────────

function renderMonitor(data) {
  if (!data || !data.system) { console.warn('[renderMonitor] no system data'); return; }
  const { system, services, processes, network } = data;

  // Each section wrapped in try/catch — single card failure won't break others
  try {
    const cpu = system.cpu;
    $('cpu-val').textContent = cpu.percent.toFixed(1) + '%';
    const cpuBar = $('cpu-bar');
    cpuBar.style.width = cpu.percent + '%';
    cpuBar.className = 'bf ' + barColor(cpu.percent);
    const cores = cpu.per_core || [];
    if (cores.length > 0) {
      const mx = Math.max(...cores), mn = Math.min(...cores);
      $('cpu-detail').textContent = `${cores.length}核 ↑${mx.toFixed(0)}% ↓${mn.toFixed(0)}%`;
    } else {
      $('cpu-detail').textContent = '';
    }
    if (cpu.temp) $('cpu-detail').textContent += ` 🌡${cpu.temp}°C`;
  } catch(e) { console.warn('[renderMonitor] CPU failed:', e); }

  try {
    const mem = system.memory;
    $('mem-val').textContent = mem.percent + '%';
    const memBar = $('mem-bar');
    memBar.style.width = mem.percent + '%';
    memBar.className = 'bf b-blue';
    $('mem-detail').textContent = fmtBytes(mem.used) + ' / ' + fmtBytes(mem.total);
  } catch(e) { console.warn('[renderMonitor] Memory failed:', e); }

  try {
    const disk = system.disk;
    $('disk-val').textContent = disk.percent + '%';
    const diskBar = $('disk-bar');
    diskBar.style.width = disk.percent + '%';
    diskBar.className = 'bf ' + barColor(disk.percent);
    $('disk-detail').textContent = fmtBytes(disk.used) + ' / ' + fmtBytes(disk.total);
  } catch(e) { console.warn('[renderMonitor] Disk failed:', e); }

  try {
    const ld = system.cpu.load;
    $('load-val').textContent = ld['1m'];
    $('load-detail').textContent = `1m: ${ld['1m']}  5m: ${ld['5m']}`;
  } catch(e) { console.warn('[renderMonitor] Load failed:', e); }

  try {
    $('uptime-val').textContent = fmtUptime(system.uptime);
  } catch(e) {}

  try {
    const net = network || {};
    $('net-up').textContent = fmtSpeed(net.speed_up || 0);
    $('net-down').textContent = fmtSpeed(net.speed_down || 0);
    const quota = 10995116277760; // 10 TB — OCI only counts outbound
    if (net.monthly) {
      const tx = net.monthly.tx || 0;
      const pct = (tx / quota * 100).toFixed(1);
      $('net-usage').textContent = `${fmtBytes(tx)} / ${fmtBytes(quota)}`;
    } else if (net.boot) {
      const tx = net.boot.tx || 0;
      $('net-usage').textContent = `${fmtBytes(tx)} / ${fmtBytes(quota)} (重启后)`;
    }
  } catch(e) { console.warn('[renderMonitor] Network failed:', e); }

  try {
    if (services) {
      const names = { hermes: 'Hermes', gateway: 'Gateway', nginx: 'Nginx' };
      $('services-card').innerHTML = Object.entries(services).map(([k,v]) =>
        `<div class="svc-row">
          <span><span class="svc-dot ${v?'svc-on':'svc-off'}"></span>${names[k]||k}</span>
          <span style="color:${v?'var(--success)':'var(--danger)'};font-size:11px">${v?'● 运行中':'○ 未运行'}</span>
        </div>`
      ).join('');
    }
  } catch(e) { console.warn('[renderMonitor] Services failed:', e); }

  try {
    const oci = data.oci;
    if (oci) {
      if (oci.enabled === false) {
        $('oci-card').innerHTML = '<div class="tr"><span>☁️ OCI</span><span style="color:var(--hint);font-size:11px">未配置</span></div>';
      } else if (oci.error && !oci.services) {
        $('oci-card').innerHTML = `<div class="tr"><span>☁️ OCI</span><span style="color:var(--danger);font-size:11px">${esc(oci.error)}</span></div>`;
      } else {
        const cur = oci.currency || 'USD';
        const rows = (oci.services||[]).map(s =>
          `<div class="tr"><span>${esc(s.service)}</span><span>${s.amount.toFixed(2)} ${cur}</span></div>`
        ).join('');
        let totalLine = `<div class="tr" style="font-weight:600;border-top:1px solid rgba(255,255,255,0.1)"><span>💰 总计</span><span style="color:var(--accent)">${oci.total.toFixed(2)} ${cur}</span></div>`;
        if (oci.delta !== null && oci.delta !== undefined && oci.delta >= 0) {
          totalLine += `<div class="tr" style="font-size:10px;color:var(--hint);border:none"><span>📈 较上次</span><span>+${oci.delta.toFixed(2)} ${cur}</span></div>`;
        }
        $('oci-card').innerHTML = rows +
          totalLine +
          `<div class="tr" style="font-size:10px;color:var(--hint);border:none"><span>📅 ${oci.period}</span><span>🔄 本月至今</span></div>`;
      }
    }
  } catch(e) { console.warn('[renderMonitor] OCI failed:', e); }

  try {
    if (processes) {
      const truncate = (s, max) => s.length > max ? s.slice(0, max) + '…' : s;
      $('cpu-top').innerHTML = (processes.top_cpu||[]).map(p =>
        `<div class="proc"><span class="proc-n">${esc(truncate(p.name, 18))}</span><span class="proc-cpu">${p.cpu}%</span></div>`
      ).join('') || '<div style="color:var(--hint);font-size:11px">无数据</div>';
      $('mem-top').innerHTML = (processes.top_mem||[]).map(p =>
        `<div class="proc"><span class="proc-n">${esc(truncate(p.name, 18))}</span><span class="proc-mem">${p.mem_mb.toFixed(0)} MB</span></div>`
      ).join('') || '<div style="color:var(--hint);font-size:11px">无数据</div>';
    }
  } catch(e) { console.warn('[renderMonitor] Processes failed:', e); }
}

// ── Hermes Page Data (loaded on tab switch, refreshed periodically) ──

let hermesRefreshTimer = null;

async function loadHermesData() {
  try {
    const r = await fetch(`${API}/hermes/overview`);
    const d = await r.json();

    // Model
    if (d.model) {
      $('hermes-model').textContent = d.model.model || '--';
      $('hermes-provider').textContent = 'Provider: ' + (d.model.provider || '--');
      $('header-model').textContent = '🤖 ' + (d.model.model && d.model.model.length > 20 ? d.model.model.slice(0,20)+'…' : d.model.model || '--');
    }

    // Platforms — skip if unchanged to prevent SVG flicker
    if (d.platforms) {
      const icons = {
        Telegram: '<img src="/static/icons/telegram.svg" class="platform-svg">',
        QQ: '<img src="/static/icons/qq.svg" class="platform-svg">',
        '微信': '<img src="/static/icons/wechat.svg" class="platform-svg">',
      };
      const html = Object.entries(d.platforms||{}).map(([n,v]) =>
        `<div class="platform-item">
          <div class="platform-icon">${icons[n]||'📡'}</div>
          <div class="platform-name">${n}</div>
          <div class="platform-status ${v?'online':'offline'}">${v?'在线':'离线'}</div>
        </div>`
      ).join('');
      if ($('platform-grid').innerHTML !== html) {
        $('platform-grid').innerHTML = html;
      }
    }

    // Memory
    if (d.memory) {
      $('mem-count').textContent = d.memory.count ?? '--';
      $('mem-writes').textContent = d.memory.today_writes ?? 0;
      $('mem-queries').textContent = d.memory.today_queries ?? 0;
    }

    // Engines (LLM + Memory)
    if (d.engines) {
      const ed = d.engines;
      $('eng-llm').textContent = ed.llm?.name || '--';
      $('eng-llm-sub').textContent = ed.llm?.provider || '';
      const memStatus = ed.memory?.status === 'healthy' ? '🟢 正常' : ed.memory?.status === 'degraded' ? '🟡 降级' : ed.memory?.status || '--';
      $('eng-memory').textContent = ed.memory?.provider === 'openviking' ? 'OpenViking' : (ed.memory?.provider || '--');
      $('eng-memory-sub').textContent = memStatus;
    }

    // Local models (redesigned vector + retrieval cards)
    if (d.local_models) {
      renderLocalModels(d.local_models);
    }
  } catch(_) {}
}

// ── Local Models Renderer (pure render, no fetch) ─────────────────
const origSwitch = switchTab;
switchTab = function(tab) {
  origSwitch(tab);
  if (tab === 'hermes') {
    loadHermesData();
    if (hermesRefreshTimer) clearInterval(hermesRefreshTimer);
    hermesRefreshTimer = setInterval(loadHermesData, 30000);
  } else {
    if (hermesRefreshTimer) { clearInterval(hermesRefreshTimer); hermesRefreshTimer = null; }
  }
};

// ── Local Models Renderer (pure render, no fetch) ─────────────────

function fmtTimeAgo(isoStr) {
  if (!isoStr) return '从未';
  const then = new Date(isoStr);
  const now = new Date();
  const diff = (now - then) / 1000;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
  if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
  return Math.floor(diff / 86400) + ' 天前';
}

function fmtEstTime(queuePending, avgLatencyMs) {
  if (queuePending <= 0) return null;
  if (avgLatencyMs <= 0) return `约 ${queuePending} 项待处理`;
  const totalMs = queuePending * avgLatencyMs;
  if (totalMs < 1000) return `≈ ${Math.round(totalMs)}ms`;
  if (totalMs < 60000) return `≈ ${(totalMs/1000).toFixed(1)}s`;
  return `≈ ${Math.round(totalMs/60000)}m ${Math.round((totalMs%60000)/1000)}s`;
}

async function renderLocalModels(lmData) {
  try {
    const d = lmData;

    // ── Embedding Model Card ──
    const emb = d.embedding || {};
    $('emb-model').textContent = emb.model || '--';

    // Status badge
    const embBadge = $('emb-badge');
    const status = emb.status || 'offline';
    embBadge.className = 'lm-badge ' + status;
    const statusMap = {
      idle: '● 空闲',
      processing: '⚡ 处理中',
      queued: '⏳ 队列中',
      offline: '○ 离线',
      active: '● 在线',
    };
    embBadge.textContent = statusMap[status] || '● ' + status;

    $('emb-calls').textContent = emb.calls || 0;
    $('emb-tokens').textContent = (emb.tokens || 0) + ' tokens';
    $('emb-last').textContent = fmtTimeAgo(emb.last_used);

    // Queue showing (always visible)
    const qPend = emb.queue_pending || 0;
    const qAct = emb.queue_active || 0;
    if (qPend > 0 || qAct > 0) {
      $('emb-queue').textContent = `${qPend} 待处理 · ${qAct} 执行中`;
    } else {
      $('emb-queue').textContent = `0 待处理`;
    }

    // Estimated time
    const avgLat = emb.avg_latency_ms || 0;
    const est = fmtEstTime(qPend, avgLat);
    if (est) {
      $('emb-conf-row').style.display = 'flex';
      $('emb-est').textContent = est;
    } else {
      $('emb-conf-row').style.display = 'none';
    }

    // ── VLM Model Card ──
    const vlm = d.vlm || {};
    $('vlm-model').textContent = vlm.model || '--';

    const vlmBadge = $('vlm-badge');
    let vlmStatus = vlm.status || 'offline';
    // Override loaded status based on queue activity
    const vqPend = vlm.queue_pending || 0;
    const vqAct = vlm.queue_active || 0;
    if (vlmStatus === 'loaded' || vlmStatus === 'online') {
      if (vqAct > 0) { vlmStatus = 'working'; }
      else if (vqPend > 0) { vlmStatus = 'queued'; }
      else { vlmStatus = 'idle'; }
    }
    vlmBadge.className = 'lm-badge ' + vlmStatus;
    const vlmStatusMap = {
      idle: '● 空闲',
      working: '⚡ 工作中',
      queued: '⏳ 队列中',
      unloaded: '● 未加载',
      offline: '○ 离线',
      online: '● 在线',
      active: '● 在线',
    };
    vlmBadge.textContent = vlmStatusMap[vlmStatus] || '● ' + vlmStatus;

    $('vlm-queries').textContent = vlm.queries || 0;
    $('vlm-latency').textContent = vlm.avg_latency_ms ? vlm.avg_latency_ms + 'ms' : '--';

    $('vlm-last').textContent = fmtTimeAgo(vlm.last_used);

    // Queue showing (always visible)
    if (vqPend > 0 || vqAct > 0) {
      $('vlm-queue').textContent = `${vqPend} 待处理 · ${vqAct} 执行中`;
    } else {
      $('vlm-queue').textContent = `0 待处理`;
    }

    // Estimated response time
    const vAvgLat = vlm.avg_latency_ms || 0;
    const vEst = fmtEstTime(vqPend, vAvgLat);
    if (vEst) {
      $('vlm-conf-row').style.display = 'flex';
      $('vlm-est').textContent = vEst;
    } else {
      $('vlm-conf-row').style.display = 'none';
    }
  } catch(_) {}
}

// ── Ops Access Control ──────────────────────────────────────────────

async function checkOpsAccess() {
  try {
    const r = await fetch(`${API}/ops/check-access`);
    const d = await r.json();
    if (d.allowed) {
      opsAuth = true;
      $('ops-panel').style.display = 'block';
      $('ops-login').style.display = 'none';
      loadCronJobs();
    } else {
      $('ops-login').style.display = 'block';
      $('ops-msg').textContent = `您的 IP (${d.ip}) 需密码验证`;
    }
  } catch(_) {
    $('ops-login').style.display = 'block';
    $('ops-msg').textContent = '无法验证，请输入密码';
  }
}

async function loadCronJobs() {
  const list = $('cron-jobs-list');
  list.innerHTML = '<div style="font-size:12px;color:var(--hint);padding:8px 12px">⏳ 加载中...</div>';
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 5000);
    const r = await fetch(`${API}/cron/jobs`, { signal: ctrl.signal });
    clearTimeout(timer);
    const d = await r.json();
    if (!d.jobs || d.jobs.length === 0) {
      list.innerHTML = '<div style="font-size:12px;color:var(--hint);padding:8px 12px">暂无定时任务</div>';
      return;
    }
    list.innerHTML = d.jobs.map(j => {
      const s = j.status || 'unknown';
      const cls = s === 'ok' ? 'status-ok' : s === 'error' ? 'status-error' : 'status-unknown';
      const dot = s === 'ok' ? 'dot-ok' : s === 'error' ? 'dot-error' : 'dot-unknown';
      const label = s === 'ok' ? '成功' : s === 'error' ? '失败' : '未启动';
      const sub = j.last_run ? fmtTimeAgo(j.last_run) : '';
      return `<div class="cron-job-row">
        <span class="cron-job-dot ${dot}"></span>
        <div class="cron-job-info">
          <div class="cron-job-name">${j.name || j.id}</div>
          ${sub ? `<div class="cron-job-sub">${sub}</div>` : ''}
        </div>
        <span class="cron-job-status ${cls}">${label}</span>
      </div>`;
    }).join('');
  } catch(e) {
    const msg = e.name === 'AbortError' ? '加载超时，请检查 Hermes 服务' : '加载失败';
    list.innerHTML = `<div style="font-size:12px;color:var(--danger);padding:8px 12px">${msg}</div>`;
  }
}

async function verifyOps() {
  const pwd = $('ops-pwd').value.trim();
  if (!pwd) return;
  try {
    const r = await fetch(`${API}/ops/verify-password`, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({password: pwd})
    });
    const d = await r.json();
    if (d.success) {
      opsAuth = true; opsPwd = pwd;
      $('ops-panel').style.display = 'block';
      $('ops-login').style.display = 'none';
      logOps('✅ 验证成功');
      loadCronJobs();
    } else {
      $('ops-pwd').value = '';
      $('ops-pwd').style.borderColor = '#ef4444';
      setTimeout(() => $('ops-pwd').style.borderColor = '', 1500);
    }
  } catch(_) {}
}

const OPS_LOG_MAX = 10;
let opsLogs = [];

function logOps(msg, type = 'info') {
  const time = new Date().toLocaleTimeString('zh-CN');
  opsLogs.unshift(`[${time}] ${msg}`);
  if (opsLogs.length > OPS_LOG_MAX) opsLogs.pop();
  $('ops-log').innerHTML = opsLogs.map(m => `<div style="padding:3px 0">${m}</div>`).join('');
}

async function emergencyRestart() {
  if (!confirm('⚠️ 确定执行紧急重启？\n\n将杀掉所有 Hermes 进程并重启 Gateway。')) return;
  const btn = document.getElementById('btn-emergency');
  if (btn && btn.disabled) return;
  if (btn) { btn.disabled = true; btn.textContent = '执行中...'; }
  logOps('🚨 执行紧急重启...');
  try {
    const body = opsPwd ? { ops_password: opsPwd } : {};
    const r = await fetch(`${API}/ops/emergency-restart`, {
      method: 'POST',
      headers: {'X-API-Token': TOKEN, 'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    const d = await r.json();
    (d.results||[]).forEach(res => logOps(res));
  } catch(e) { logOps('❌ ' + e.message, 'error'); }
  finally { if (btn) { setTimeout(() => { btn.disabled = false; btn.textContent = '🚨 强制重启'; }, 3000); } }
}

async function restartSvc(service) {
  const btn = document.getElementById('btn-restart-' + service);
  if (btn && btn.disabled) return;
  if (btn) { btn.disabled = true; btn.textContent = '重启中...'; }
  logOps(`🔄 重启 ${service}...`);
  try {
    const body = { service };
    if (opsPwd) body.ops_password = opsPwd;
    const r = await fetch(`${API}/ops/restart-service`, {
      method: 'POST',
      headers: {'X-API-Token': TOKEN, 'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    const d = await r.json();
    logOps(d.exit_code === 0 ? `✅ ${service} 重启成功` : `❌ ${service} 重启失败`, d.exit_code === 0 ? 'success' : 'error');
  } catch(e) { logOps('❌ ' + e.message, 'error'); }
  finally { if (btn) { setTimeout(() => { btn.disabled = false; btn.textContent = `重启 ${service}`; }, 3000); } }
}

// ── Init ─────────────────────────────────────────────────────────────

// SSE real-time metrics stream with exponential backoff
let _sseRetryDelay = 3000;
const _sseMaxDelay = 30000;
function connectMonitorStream() {
  const es = new EventSource('/api/stream?interval=5');
  es.addEventListener('metrics', (e) => {
    _sseRetryDelay = 3000; // reset on success
    try {
      const data = JSON.parse(e.data);
      renderMonitor(data);
    } catch(_) {}
  });
  es.addEventListener('error', () => {
    es.close();
    const jitter = Math.random() * 1000;
    setTimeout(connectMonitorStream, _sseRetryDelay + jitter);
    _sseRetryDelay = Math.min(_sseRetryDelay * 1.5, _sseMaxDelay);
  });
}
connectMonitorStream();

checkOpsAccess();
// Preload engine data for smooth tab switching
loadHermesData();

// ── Chat ────────────────────────────────────────────────
let chatActive = null; // current EventSource for streaming

function appendChatMsg(role, text, meta) {
  const box = document.getElementById('chat-messages');
  const div = document.createElement('div');
  div.className = 'chat-msg ' + role;
  const content = role === 'bot' ? marked.parse(text) : escapeHtml(text);
  div.innerHTML = `<div class="chat-bubble">${content}</div>${meta?`<div class="chat-meta">${meta}</div>`:''}`;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function escapeHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

async function sendChat() {
  const input = document.getElementById('chat-input');
  const btn   = document.getElementById('chat-send-btn');
  const msg   = input.value.trim();
  if (!msg || sendChat.busy) return;

  sendChat.busy = true;
  appendChatMsg('user', msg);
  input.value = '';
  btn.disabled = true;

  // placeholder for bot
  const placeholder = document.createElement('div');
  placeholder.className = 'chat-msg bot';
  const bubble = document.createElement('div');
  bubble.className = 'chat-bubble chat-typing';
  bubble.textContent = '正在思考...';
  placeholder.appendChild(bubble);
  document.getElementById('chat-messages').appendChild(placeholder);
  document.getElementById('chat-messages').scrollTop = 1e9;

  let fullText = '';
  let firstToken = true;

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: msg}),
    });

    if (!resp.ok) throw new Error('HTTP ' + resp.status);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      const chunk = decoder.decode(value, {stream: true});
      buffer += chunk;
      // SSE blocks are separated by blank lines (\n\n or \r\n\r\n)
      const parts = buffer.split(/\r?\n\r?\n/);
      buffer = parts.pop() || '';
      for (const part of parts) {
        let etype = '', edata = '';
        for (const line of part.split(/\r?\n/)) {
          if (line.startsWith('event:')) etype = line.slice(6).trim();
          else if (line.startsWith('data:')) edata = line.slice(5);
        }
        if (etype === 'token' && edata) {
          if (firstToken) {
            bubble.className = 'chat-bubble';
            bubble.textContent = '';
            firstToken = false;
          }
          fullText += edata;
          bubble.textContent = fullText;
        } else if (etype === 'done') {
          if (firstToken && !fullText) {
            bubble.className = 'chat-bubble';
            bubble.textContent = '（无输出）';
          }
        } else if (etype === 'error') {
          bubble.className = 'chat-bubble';
          bubble.style.color = 'var(--danger)';
          bubble.textContent = '错误: ' + edata;
        }
      }
      document.getElementById('chat-messages').scrollTop = 1e9;
    }
    // If stream ended without any token
    if (firstToken && !fullText) {
      bubble.className = 'chat-bubble';
      bubble.textContent = '（无输出）';
    }
  } catch(e) {
    bubble.className = 'chat-bubble';
    bubble.style.color = 'var(--danger)';
    bubble.textContent = '错误: ' + e.message;
  } finally {
    btn.disabled = false;
    sendChat.busy = false;
    document.getElementById('chat-messages').scrollTop = 1e9;
  }
}
