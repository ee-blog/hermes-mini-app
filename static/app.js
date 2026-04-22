const API = '/api';
        const TOKEN = 'hermes-mini-app-2024';
        const tg = window.Telegram?.WebApp;
        if (tg) { tg.expand(); tg.setHeaderColor('#1a1a2e'); }

        function switchTab(tab) {
            document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.getElementById('page-' + tab).classList.add('active');
            event.currentTarget.classList.add('active');
        }

        function barColor(p) {
            if (p > 85) return 'fill-red';
            if (p > 60) return 'fill-yellow';
            return 'fill-green';
        }

        function formatUptime(seconds) {
            const d = Math.floor(seconds / 86400);
            const h = Math.floor((seconds % 86400) / 3600);
            if (d > 0) return d + 'd ' + h + 'h';
            const m = Math.floor((seconds % 3600) / 60);
            if (h > 0) return h + 'h ' + m + 'm';
            return m + 'm';
        }

        function formatBytes(bytes) {
            if (bytes === 0) return '0 B';
            const k = 1024;
            const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
            const i = Math.floor(Math.log(bytes) / Math.log(k));
            return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
        }

        function formatSpeed(bytesPerSec) {
            return formatBytes(bytesPerSec) + '/s';
        }

        function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

        // === Monitor ===
        async function refreshMonitor() {
            try {
                const r = await fetch(API + '/system');
                const d = await r.json();

                document.getElementById('cpu-val').textContent = d.cpu.percent.toFixed(1) + '%';
                const cpuBar = document.getElementById('cpu-bar');
                cpuBar.style.width = d.cpu.percent + '%';
                cpuBar.className = 'gauge-fill ' + barColor(d.cpu.percent);
                // CPU per-core: compact one line
                const cores = d.cpu.per_core || [];
                document.getElementById('cpu-detail').textContent = cores.map((v, i) => `C${i+1}:${v.toFixed(0)}%`).join(' ');

                document.getElementById('mem-val').textContent = d.memory.percent.toFixed(1) + '%';
                const memBar = document.getElementById('mem-bar');
                memBar.style.width = d.memory.percent + '%';
                memBar.className = 'gauge-fill ' + (d.memory.percent > 80 ? 'fill-red' : 'fill-blue');
                document.getElementById('mem-detail').textContent = formatBytes(d.memory.used) + ' / ' + formatBytes(d.memory.total);

                document.getElementById('disk-val').textContent = d.disk.percent.toFixed(1) + '%';
                const diskBar = document.getElementById('disk-bar');
                diskBar.style.width = d.disk.percent + '%';
                diskBar.className = 'gauge-fill ' + barColor(d.disk.percent);
                document.getElementById('disk-detail').textContent = formatBytes(d.disk.used) + ' / ' + formatBytes(d.disk.total);

                document.getElementById('load-val').textContent = d.cpu.load1;
                document.getElementById('load-detail').textContent = `1m: ${d.cpu.load1}  5m: ${d.cpu.load5}`;
                document.getElementById('stat-uptime').textContent = formatUptime(d.uptime);

                // Net speed
                if (d.io && d.io.net_speed) {
                    document.getElementById('net-up').textContent = formatSpeed(d.io.net_speed.upload_per_sec);
                    document.getElementById('net-down').textContent = formatSpeed(d.io.net_speed.download_per_sec);
                }
            } catch(e) { console.error(e); }
        }

        async function refreshMonthlyTraffic() {
            try {
                const r = await fetch(API + '/network');
                const d = await r.json();
                const MONTHLY_QUOTA = 10 * 1024 * 1024 * 1024 * 1024; // 10TB
                
                if (d.monthly) {
                    const rx = formatBytes(d.monthly.rx);
                    const tx = formatBytes(d.monthly.tx);
                    const total = d.monthly.rx + d.monthly.tx;
                    const used = formatBytes(total);
                    const percent = ((total / MONTHLY_QUOTA) * 100).toFixed(1);
                    document.getElementById('net-monthly').textContent = `↓${rx} ↑${tx}`;
                    document.getElementById('net-quota').textContent = `${used} / 10 TB (${percent}%)`;
                } else if (d.boot_total) {
                    const rx = formatBytes(d.boot_total.rx);
                    const tx = formatBytes(d.boot_total.tx);
                    const total = d.boot_total.rx + d.boot_total.tx;
                    const used = formatBytes(total);
                    const percent = ((total / MONTHLY_QUOTA) * 100).toFixed(1);
                    const up = formatUptime(d.boot_total.uptime);
                    document.getElementById('net-monthly').textContent = `开机: ↓${rx} ↑${tx} (${up})`;
                    document.getElementById('net-quota').textContent = `${used} / 10 TB (${percent}%)`;
                }
            } catch(e) {}
        }

        async function refreshProcesses() {
            try {
                const r = await fetch(API + '/processes');
                const d = await r.json();
                
                document.getElementById('cpu-top').innerHTML = (d.top_cpu || []).map(p => `
                    <div class="proc-item">
                        <span class="proc-name">${esc(p.name)}</span>
                        <span class="proc-cpu">${p.cpu}%</span>
                    </div>
                `).join('') || '<div style="color:var(--hint);font-size:11px">无数据</div>';
                
                document.getElementById('mem-top').innerHTML = (d.top_mem || []).map(p => {
                    return `<div class="proc-item">
                        <span class="proc-name">${esc(p.name)}</span>
                        <span class="proc-mem">${p.mem_mb.toFixed(0)} MB</span>
                    </div>`;
                }).join('') || '<div style="color:var(--hint);font-size:11px">无数据</div>';
            } catch(e) {}
        }

        async function refreshServices() {
            try {
                const r = await fetch(API + '/services');
                const d = await r.json();
                const names = {
                    hermes: 'Hermes',
                    gateway: 'Gateway',
                    ollama: 'Ollama'
                };
                document.getElementById('services-card').innerHTML = Object.entries(d).map(([k, v]) => `
                    <div class="info-row">
                        <span>${names[k] || k}</span>
                        <span style="color:${v ? 'var(--success)' : 'var(--danger)'}">● ${v ? '运行中' : '未运行'}</span>
                    </div>
                `).join('');
            } catch(e) {}
        }

        // === Hermes ===
        async function loadHermesData() {
            let modelInfo = { model: '--', provider: '--' };
            let memInfo = { provider: '--', count: 0 };
            
            // Model
            try {
                const r = await fetch(API + '/hermes/model');
                const d = await r.json();
                modelInfo = { model: d.model || '--', provider: d.provider || '--' };
                document.getElementById('model-name').textContent = d.model || '--';
                document.getElementById('model-provider').textContent = 'Provider: ' + (d.provider || '--');
            } catch(e) {}

            // Platforms
            try {
                const r = await fetch(API + '/hermes/platforms');
                const d = await r.json();
                const icons = { Telegram: '✈️', QQ: '🐧', '微信': '💬' };
                document.getElementById('platform-grid').innerHTML = Object.entries(d.platforms || {}).map(([name, online]) => `
                    <div class="platform-item">
                        <div class="platform-icon">${icons[name] || '📡'}</div>
                        <div class="platform-name">${name}</div>
                        <div class="platform-status ${online ? 'online' : 'offline'}">${online ? '在线' : '离线'}</div>
                    </div>
                `).join('');
            } catch(e) {}

            // Memory count
            try {
                const mr = await fetch(API + '/hermes/memory');
                const md = await mr.json();
                memInfo = { provider: md.provider || '--', count: md.count || 0 };
                document.getElementById('stat-memory').textContent = md.count || '--';
                document.getElementById('stat-mem-queries').textContent = md.today_queries || 0;
                document.getElementById('stat-mem-writes').textContent = md.today_writes || 0;
                
                // 控制动画：值为0时停止
                const writeBridge = document.getElementById('write-bridge');
                const queryBridge = document.getElementById('query-bridge');
                const brain = document.getElementById('stat-brain');
                
                const hasWrites = (md.today_writes || 0) > 0;
                const hasQueries = (md.today_queries || 0) > 0;
                const hasActivity = hasWrites || hasQueries;
                
                writeBridge?.classList.toggle('idle', !hasWrites);
                queryBridge?.classList.toggle('idle', !hasQueries);
                brain?.classList.toggle('idle', !hasActivity);
            } catch(e) {}
            
            // 驱动引擎
            let embedInfo = { engine: '--', models: [], vector_db: '--', active: false };
            try {
                const er = await fetch(API + '/hermes/embedding');
                const ed = await er.json();
                embedInfo = ed;
            } catch(e) {}

            const embedModel = embedInfo.models.length > 0 
                ? embedInfo.models.map(m => m.name).join(', ')
                : (embedInfo.active ? '无' : '离线');

            const engines = [
                { name: 'LLM', value: modelInfo.model, icon: '🤖' },
                { name: '记忆', value: memInfo.provider, icon: '💾' },
                { name: '向量引擎', value: embedInfo.engine + ' · ' + embedModel, icon: '🧮' },
                { name: '向量库', value: embedInfo.vector_db, icon: '🔎' },
            ];
            document.getElementById('engine-list').innerHTML = engines.map(e => `
                <div class="info-row">
                    <span>${e.icon} ${e.name}</span>
                    <span style="color:var(--accent);font-size:12px">${e.value}</span>
                </div>
            `).join('');
        }

        // === Maintenance ===
        let opsHistory = [];

        function logOps(msg, type = 'info') {
            const time = new Date().toLocaleTimeString('zh-CN');
            opsHistory.unshift(`[${time}] ${msg}`);
            if (opsHistory.length > 10) opsHistory.pop();
            const colors = { info: 'var(--text)', success: 'var(--success)', error: 'var(--danger)' };
            document.getElementById('ops-log').innerHTML = opsHistory.map(m => 
                `<div style="color:${colors[type] || 'var(--text)'};font-size:12px;padding:4px 0">${m}</div>`
            ).join('');
        }

        async function emergencyRestart() {
            if (!confirm('⚠️ 确定要执行紧急重启吗？\n\n这将杀掉所有 Hermes 相关进程并重启 Gateway。')) return;
            logOps('🚨 执行紧急重启...', 'info');
            try {
                const body = opsPassword ? { ops_password: opsPassword } : {};
                const r = await fetch(API + '/ops/emergency-restart', {
                    method: 'POST',
                    headers: { 'X-API-Token': TOKEN, 'Content-Type': 'application/json' },
                    body: JSON.stringify(body)
                });
                const d = await r.json();
                if (d.results) {
                    d.results.forEach(res => logOps(res, res.includes('✅') ? 'success' : 'info'));
                    logOps('✅ 紧急重启完成', 'success');
                } else {
                    logOps('❌ ' + (d.error || '未知错误'), 'error');
                }
            } catch(e) {
                logOps('❌ 请求失败: ' + e.message, 'error');
            }
        }

        async function restartService(service) {
            logOps(`🔄 重启 ${service}...`, 'info');
            try {
                const body = { service };
                if (opsPassword) body.ops_password = opsPassword;
                const r = await fetch(API + '/ops/restart-service', {
                    method: 'POST',
                    headers: { 'X-API-Token': TOKEN, 'Content-Type': 'application/json' },
                    body: JSON.stringify(body)
                });
                const d = await r.json();
                if (d.exit_code === 0) {
                    logOps(`✅ ${service} 重启成功`, 'success');
                } else {
                    logOps(`❌ ${service} 重启失败`, 'error');
                }
            } catch(e) {
                logOps('❌ 请求失败: ' + e.message, 'error');
            }
        }

        // === Init ===
        refreshMonitor();
        refreshProcesses();
        refreshServices();
        refreshMonthlyTraffic();
        loadHermesData();
        checkOpsAccess();

        setInterval(refreshMonitor, 5000);
        setInterval(refreshProcesses, 5000);
        setInterval(refreshServices, 15000);
        setInterval(refreshMonthlyTraffic, 30000);
        setInterval(loadHermesData, 30000);

        // 当前时间显示（每秒更新）
        function updateCurrentTime() {
            document.getElementById('current-time').textContent = new Date().toLocaleTimeString('zh-CN');
        }
        updateCurrentTime();
        setInterval(updateCurrentTime, 1000);

        // === 维护页面访问控制 ===
        let opsAuthenticated = false;
        let opsPassword = '';

        async function checkOpsAccess() {
            try {
                const r = await fetch(API + '/ops/check-access');
                const d = await r.json();
                if (d.allowed) {
                    // IP 白名单，直接显示操作面板
                    opsAuthenticated = true;
                    document.getElementById('ops-panel').style.display = 'block';
                    document.getElementById('ops-login').style.display = 'none';
                } else {
                    // 需要密码验证
                    document.getElementById('ops-login').style.display = 'block';
                    document.getElementById('ops-panel').style.display = 'none';
                    document.getElementById('ops-ip-msg').textContent = `您的 IP (${d.ip}) 无权访问，请输入密码`;
                }
            } catch(e) {
                document.getElementById('ops-login').style.display = 'block';
                document.getElementById('ops-ip-msg').textContent = '无法验证访问权限，请输入密码';
            }
        }

        async function submitOpsPassword() {
            const input = document.getElementById('ops-password-input');
            const pwd = input.value.trim();
            if (!pwd) return;
            try {
                const r = await fetch(API + '/ops/verify-password', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({password: pwd})
                });
                const d = await r.json();
                if (d.success) {
                    opsAuthenticated = true;
                    opsPassword = pwd;
                    document.getElementById('ops-panel').style.display = 'block';
                    document.getElementById('ops-login').style.display = 'none';
                } else {
                    input.value = '';
                    input.style.borderColor = '#ef4444';
                    setTimeout(() => input.style.borderColor = '', 1500);
                }
            } catch(e) {}
        }

        // === Gemma 聊天 ===
        let chatBusy = false;

        async function sendChat() {
            if (chatBusy || !opsAuthenticated) return;
            const input = document.getElementById('chat-input');
            const msg = input.value.trim();
            if (!msg) return;

            const messagesEl = document.getElementById('chat-messages');
            const btn = document.getElementById('chat-send-btn');
            const statusEl = document.getElementById('chat-status');

            // 显示用户消息
            messagesEl.innerHTML += `<div class="chat-msg user">${esc(msg)}</div>`;
            input.value = '';
            chatBusy = true;
            btn.disabled = true;

            // 创建 assistant 消息容器
            const assistantDiv = document.createElement('div');
            assistantDiv.className = 'chat-msg assistant';
            messagesEl.appendChild(assistantDiv);

            statusEl.textContent = '思考中...';

            try {
                const body = { message: msg };
                if (opsPassword) body.ops_password = opsPassword;

                const r = await fetch(API + '/chat/gemma', {
                    method: 'POST',
                    headers: { 'X-API-Token': TOKEN, 'Content-Type': 'application/json' },
                    body: JSON.stringify(body)
                });

                if (!r.ok) {
                    let errMsg = '请求失败 (' + r.status + ')';
                    try { const err = await r.json(); errMsg = err.error || errMsg; } catch(_) {}
                    assistantDiv.textContent = '❌ ' + errMsg;
                    statusEl.textContent = '';
                    chatBusy = false;
                    btn.disabled = false;
                    return;
                }

                // SSE 流式读取
                const reader = r.body.getReader();
                const decoder = new TextDecoder();
                let buffer = '';

                statusEl.textContent = '回复中...';

                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    buffer += decoder.decode(value, { stream: true });

                    // 解析 SSE data: 行
                    const lines = buffer.split('\n');
                    buffer = lines.pop() || '';

                    for (const line of lines) {
                        if (line.startsWith('data: ')) {
                            try {
                                const data = JSON.parse(line.slice(6));
                                if (data.content) {
                                    assistantDiv.textContent += data.content;
                                    messagesEl.scrollTop = messagesEl.scrollHeight;
                                }
                                if (data.done) {
                                    statusEl.textContent = '完成';
                                    setTimeout(() => statusEl.textContent = '', 2000);
                                }
                                if (data.error) {
                                    assistantDiv.textContent = '❌ ' + data.error;
                                    statusEl.textContent = '';
                                }
                            } catch(e) {}
                        }
                    }
                }
            } catch(e) {
                assistantDiv.textContent = '❌ 请求失败: ' + e.message;
                statusEl.textContent = '';
            }

            chatBusy = false;
            btn.disabled = false;
            messagesEl.scrollTop = messagesEl.scrollHeight;
        }
