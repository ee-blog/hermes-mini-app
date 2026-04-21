#!/usr/bin/env python3
"""Mini App Backend - System monitoring and Hermes control API"""

import os
import pwd
import subprocess
import time
import json
import re
import psutil
from flask import Flask, jsonify, request
from functools import wraps

app = Flask(__name__)

API_TOKEN=os.environ.get("MINI_APP_TOKEN", "your-secret-token-here")
HERMES_HOME = os.environ.get('HERMES_HOME', os.path.expanduser('~/.hermes'))

# 维护页面 IP 白名单
OPS_WHITELIST = {
    '127.0.0.1',
    '::1',
    '10.0.0.198',       # 服务器内网
    '217.142.188.37',   # 服务器公网
    '35.206.74.247',    # 白名单
    '45.92.195.19',     # 白名单
}

# 维护页面密码
OPS_PASSWORD = os.environ.get('OPS_PASSWORD', 'your-ops-password')

def get_client_ip():
    """获取客户端真实 IP（支持 nginx 反代）"""
    # 优先检查 nginx 传递的头
    forwarded = request.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    real_ip = request.headers.get('X-Real-IP', '')
    if real_ip:
        return real_ip.strip()
    return request.remote_addr or '127.0.0.1'

def check_ops_allowed(password=None):
    """检查是否允许访问维护页面（IP白名单 或 密码验证）"""
    client_ip = get_client_ip()
    # IP 白名单直接放行
    if client_ip in OPS_WHITELIST:
        return True, client_ip
    # 否则需要密码验证
    if password and password == OPS_PASSWORD:
        return True, client_ip
    return False, client_ip

@app.route('/api/ops/check-access')
def ops_check_access():
    """检查当前 IP 是否有权限访问维护页面"""
    client_ip = get_client_ip()
    allowed = client_ip in OPS_WHITELIST
    return jsonify({
        'ip': client_ip,
        'allowed': allowed,
        'need_password': not allowed
    })

@app.route('/api/ops/verify-password', methods=['POST'])
def ops_verify_password():
    """验证维护页面密码"""
    data = request.get_json() or {}
    password = data.get('password', '')
    allowed, client_ip = check_ops_allowed(password)
    if allowed:
        return jsonify({'success': True, 'ip': client_ip})
    return jsonify({'success': False, 'error': f'您的 IP ({client_ip}) 无权访问，密码错误'}), 403

def require_token(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('X-API-Token', '') or request.args.get('token', '')
        if token != API_TOKEN:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated

def require_ops_access(f):
    """维护操作需要 IP 白名单或密码验证"""
    @wraps(f)
    def decorated(*args, **kwargs):
        # 从请求获取密码
        data = request.get_json() or {}
        password = data.get('ops_password', '') or request.headers.get('X-Ops-Password', '')
        
        # 检查 IP 白名单或密码
        allowed, client_ip = check_ops_allowed(password if password else None)
        if not allowed:
            return jsonify({'error': f'您的 IP ({client_ip}) 无权访问维护功能'}), 403
        
        # 再检查 Token
        token = request.headers.get('X-API-Token', '') or request.args.get('token', '')
        if token != API_TOKEN:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated

def get_user_env():
    """获取用户级 systemd 需要的环境变量"""
    uid = pwd.getpwnam('ubuntu').pw_uid
    env = os.environ.copy()
    env['XDG_RUNTIME_DIR'] = f'/run/user/{uid}'
    env['HOME'] = '/home/ubuntu'
    return env

# === 系统监控 ===

# CPU 采样预热 - 非阻塞模式需要先调用一次
_psutil_cpu_preheated = False
def _preheat_cpu():
    global _psutil_cpu_preheated
    if not _psutil_cpu_preheated:
        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None, percpu=True)
        _psutil_cpu_preheated = True

# 应用启动时预热
_preheat_cpu()

@app.route('/api/system')
def system_stats():
    cpu_percent = psutil.cpu_percent(interval=None)
    cpu_per_core = psutil.cpu_percent(interval=None, percpu=True)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    net_io = psutil.net_io_counters()
    boot_time = psutil.boot_time()
    uptime = int(time.time() - boot_time)
    load1, load5, load15 = os.getloadavg()

    return jsonify({
        'cpu': {'percent': cpu_percent, 'cores': psutil.cpu_count(), 'per_core': cpu_per_core, 'load1': round(load1, 2), 'load5': round(load5, 2)},
        'memory': {'total': memory.total, 'used': memory.used, 'percent': memory.percent, 'available': memory.available},
        'disk': {'total': disk.total, 'used': disk.used, 'percent': disk.percent, 'free': disk.free},
        'network': {'bytes_sent': net_io.bytes_sent, 'bytes_recv': net_io.bytes_recv},
        'uptime': uptime,
        'io': get_cached_io_stats()
    })

# 缓存上次采样的磁盘/网络 IO 数据，用于计算速率
_io_cache = {'disk': None, 'net': None, 'time': 0}

def get_cached_io_stats():
    """获取缓存的磁盘IO和网速（无需额外sleep）"""
    global _io_cache
    now = time.time()
    d = psutil.disk_io_counters()
    n = psutil.net_io_counters()
    current = {'disk': d, 'net': n, 'time': now}
    
    result = {'disk_io': {'read_bytes_per_sec': 0, 'write_bytes_per_sec': 0},
              'net_speed': {'upload_per_sec': 0, 'download_per_sec': 0}}
    
    prev = _io_cache
    if prev.get('disk') and prev.get('time') > 0:
        dt = now - prev['time']
        if dt > 0:
            result['disk_io'] = {
                'read_bytes_per_sec': round((d.read_bytes - prev['disk'].read_bytes) / dt),
                'write_bytes_per_sec': round((d.write_bytes - prev['disk'].write_bytes) / dt)
            }
            result['net_speed'] = {
                'upload_per_sec': round((n.bytes_sent - prev['net'].bytes_sent) / dt),
                'download_per_sec': round((n.bytes_recv - prev['net'].bytes_recv) / dt)
            }
    
    _io_cache = current
    return result

@app.route('/api/network')
def network_stats():
    """网络流量详情（实时网速 + 月度统计）"""
    try:
        io = get_cached_io_stats()
        
        # 月度流量（vnstat）
        monthly = None
        try:
            result = subprocess.run(['vnstat', '--json', 'm', '-i', 'enp0s6'],
                                    capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout:
                data = json.loads(result.stdout)
                interfaces = data.get('interfaces', [])
                for iface in interfaces:
                    if iface['name'] == 'enp0s6':
                        months = iface.get('traffic', {}).get('month', [])
                        if months:
                            latest = months[-1]
                            monthly = {
                                'year': latest.get('date', {}).get('year'),
                                'month': latest.get('date', {}).get('month'),
                                'rx': latest.get('rx', 0),
                                'tx': latest.get('tx', 0)
                            }
                        break
        except:
            pass
        
        # 开机累计
        n = psutil.net_io_counters()
        uptime = int(time.time() - psutil.boot_time())
        
        return jsonify({
            'speed_up': io['net_speed']['upload_per_sec'],
            'speed_down': io['net_speed']['download_per_sec'],
            'total_sent': n.bytes_sent,
            'total_recv': n.bytes_recv,
            'monthly': monthly,
            'boot_total': {'rx': n.bytes_recv, 'tx': n.bytes_sent, 'uptime': uptime}
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/processes')
def process_list():
    psutil.cpu_percent(interval=None)
    procs = []
    for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'cpu_percent', 'memory_percent', 'memory_info']):
        try:
            info = proc.info
            cmdline = info['cmdline'] or []
            cmdline_str = ' '.join(cmdline) if cmdline else info['name']
            if len(cmdline_str) > 50:
                cmdline_str = cmdline_str[:47] + '...'
            mem_mb = info['memory_info'].rss / 1048576 if info['memory_info'] else 0
            procs.append({'pid': info['pid'], 'name': info['name'], 'cmdline': cmdline_str, 
                         'cpu': round(info['cpu_percent'] or 0, 1), 'mem_mb': round(mem_mb, 1),
                         'mem_percent': round(info['memory_percent'] or 0, 1)})
        except:
            pass
    
    # 分开排序
    top_cpu = sorted(procs, key=lambda p: p['cpu'], reverse=True)[:5]
    top_mem = sorted(procs, key=lambda p: p['mem_mb'], reverse=True)[:5]
    
    return jsonify({'top_cpu': top_cpu, 'top_mem': top_mem})

@app.route('/api/services')
def services_status():
    services = {}
    env = get_user_env()
    
    # Gateway - 用户级 systemd
    try:
        result = subprocess.run(['systemctl', '--user', 'is-active', 'hermes-gateway'],
                                capture_output=True, text=True, timeout=5, env=env)
        services['gateway'] = result.stdout.strip() == 'active'
    except:
        services['gateway'] = False
    
    # Hermes
    try:
        result = subprocess.run(['hermes', 'status'], capture_output=True, text=True, timeout=10, env=env)
        services['hermes'] = result.returncode == 0
    except:
        services['hermes'] = False
    
    # Snell
    try:
        result = subprocess.run(['systemctl', 'is-active', 'snell'],
                                capture_output=True, text=True, timeout=5)
        services['snell'] = result.stdout.strip() == 'active'
    except:
        services['snell'] = False
    
    # Ollama
    try:
        result = subprocess.run(['systemctl', 'is-active', 'ollama'],
                                capture_output=True, text=True, timeout=5)
        services['ollama'] = result.stdout.strip() == 'active'
    except:
        services['ollama'] = False
    
    return jsonify(services)

# === Hermes 控制 ===

@app.route('/api/hermes/status')
def hermes_status():
    """获取 Hermes 详细状态"""
    try:
        result = subprocess.run(['hermes', 'status'], capture_output=True, text=True, timeout=15, env=get_user_env())
        return jsonify({'output': result.stdout, 'exit_code': result.returncode})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/hermes/cron')
def hermes_cron():
    """获取定时任务详情"""
    try:
        result = subprocess.run(['hermes', 'cron', 'list'], capture_output=True, text=True, timeout=15, env=get_user_env())
        output = result.stdout
        
        # 解析 cron 任务 - 使用正则匹配
        import re as re2
        jobs = []
        
        # 分割成 job blocks (每个 job 以 job_id 开头)
        job_blocks = re2.split(r'\n(?=\s*[a-f0-9]{12}\s+\[)', output)
        
        for block in job_blocks:
            if 'Name:' not in block:
                continue
            
            # 提取各个字段
            name_match = re2.search(r'Name:\s+(.+)', block)
            sched_match = re2.search(r'Schedule:\s+(.+)', block)
            next_match = re2.search(r'Next run:\s+(.+)', block)
            last_match = re2.search(r'Last run:\s+(.+)', block)
            
            job = {
                'name': name_match.group(1).strip() if name_match else '未命名',
                'schedule': sched_match.group(1).strip() if sched_match else '',
                'next_run': next_match.group(1).strip() if next_match else '',
                'status': 'pending'  # 默认待运行
            }
            
            if last_match:
                last_run = last_match.group(1).strip()
                job['last_run'] = last_run
                if 'ok' in last_run:
                    job['status'] = 'ok'
                elif 'failed' in last_run:
                    job['status'] = 'failed'
            
            jobs.append(job)
        
        return jsonify({'jobs': jobs, 'raw': output})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/hermes/skills')
def hermes_skills():
    """获取已安装技能"""
    try:
        result = subprocess.run(['hermes', 'skills', 'list'], capture_output=True, text=True, timeout=15, env=get_user_env())
        return jsonify({'output': result.stdout})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/hermes/recent-skills')
def hermes_recent_skills():
    """获取最近使用的 skills"""
    try:
        result = subprocess.run(['hermes', 'sessions', 'list', '--json'], 
                                capture_output=True, text=True, timeout=15, env=get_user_env())
        if result.returncode == 0 and result.stdout:
            sessions = json.loads(result.stdout)
            # 找最近包含 skill 的会话
            recent_skills = []
            for s in sessions[:30]:
                title = s.get('title', '')
                # 检查标题是否包含 skill 相关关键词
                if any(kw in title.lower() for kw in ['skill', '技能', 'cron', '邮件', '新闻', 'blog', '记忆']):
                    recent_skills.append({
                        'title': title[:50],
                        'time': s.get('created_at', '')
                    })
            return jsonify({'recent_skills': recent_skills[:5]})
        return jsonify({'recent_skills': []})
    except Exception as e:
        return jsonify({'recent_skills': [], 'error': str(e)}), 500

@app.route('/api/hermes/plugins')
def hermes_plugins():
    """获取插件列表"""
    try:
        result = subprocess.run(['hermes', 'plugins', 'list'], capture_output=True, text=True, timeout=15, env=get_user_env())
        return jsonify({'output': result.stdout})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/hermes/insights')
def hermes_insights():
    """获取使用统计"""
    try:
        result = subprocess.run(['hermes', 'insights'], capture_output=True, text=True, timeout=15, env=get_user_env())
        return jsonify({'output': result.stdout})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/hermes/config')
def hermes_config():
    """获取当前配置"""
    try:
        result = subprocess.run(['hermes', 'config'], capture_output=True, text=True, timeout=15, env=get_user_env())
        return jsonify({'output': result.stdout})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/hermes/logs')
def hermes_logs():
    """获取最近日志"""
    try:
        result = subprocess.run(['hermes', 'logs', '-n', '30'], capture_output=True, text=True, timeout=15, env=get_user_env())
        return jsonify({'output': result.stdout})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/hermes/platforms')
def hermes_platforms():
    """获取平台连接状态"""
    try:
        result = subprocess.run(['hermes', 'config'], capture_output=True, text=True, timeout=15, env=get_user_env())
        output = result.stdout
        platforms = {}
        
        # 解析配置中的平台状态
        platform_patterns = [
            ('Telegram', r'Telegram:\s*(\S+)'),
            ('Discord', r'Discord:\s*(\S+)'),
        ]
        for name, pattern in platform_patterns:
            match = re.search(pattern, output)
            if match:
                platforms[name] = match.group(1) == 'configured'
        
        # 从 config.yaml 检查 QQ 是否启用
        config_path = os.path.join(HERMES_HOME, 'config.yaml')
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                config_content = f.read()
            # QQ
            qq_enabled = 'qqbot:' in config_content and 'enabled: true' in config_content
            platforms['QQ'] = qq_enabled
        
        # 微信 - 检查 .env 中是否有 WEIXIN 配置
        env_path = os.path.join(HERMES_HOME, '.env')
        if os.path.exists(env_path):
            with open(env_path, 'r') as f:
                env_content = f.read()
            platforms['微信'] = 'WEIXIN_TOKEN=' in env_content
        
        # 检查最近错误
        env = get_user_env()
        log_result = subprocess.run(
            ['journalctl', '--user', '-u', 'hermes-gateway', '-n', '50', '--no-pager'],
            capture_output=True, text=True, timeout=10, env=env
        )
        log_output = log_result.stdout
        error_lines = [l for l in log_output.split('\n') if 'ERROR' in l]
        recent_errors = error_lines[-5:] if error_lines else []
        
        # 只保留需要的平台
        result_platforms = { k: v for k, v in platforms.items() if k in ['Telegram', 'QQ', '微信'] }
        return jsonify({'platforms': result_platforms, 'recent_errors': recent_errors})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/hermes/memory')
def hermes_memory():
    """获取记忆系统状态"""
    try:
        result = subprocess.run(['hermes', 'memory'], capture_output=True, text=True, timeout=15, env=get_user_env())
        output = result.stdout
        
        # 解析状态
        status = 'unknown'
        provider = 'none'
        for line in output.split('\n'):
            if 'Status:' in line:
                status = line.split('Status:')[1].strip()
            if 'Provider:' in line:
                provider = line.split('Provider:')[1].strip()
        
        # 获取 mem0 记录数 - 从 qdrant sqlite 存储
        count = '--'
        try:
            import sqlite3
            import shutil
            import tempfile
            db_path = os.path.expanduser('~/.hermes/mem0-local-data/collection/hermes_memories/storage.sqlite')
            if os.path.exists(db_path):
                # 复制到临时文件避免锁
                fd, tmp_path = tempfile.mkstemp(suffix='.sqlite')
                os.close(fd)
                shutil.copy(db_path, tmp_path)
                conn = sqlite3.connect(tmp_path)
                cursor = conn.execute('SELECT COUNT(*) FROM points')
                count = cursor.fetchone()[0]
                conn.close()
                os.unlink(tmp_path)
        except Exception as e:
            app.logger.error(f"Memory count error: {e}")
            count = '--'
        
        # 今日记忆统计（从 session 文件统计工具调用）
        today_queries = 0
        today_writes = 0
        try:
            from datetime import datetime
            import glob
            today = datetime.now().strftime('%Y%m%d')
            sessions_dir = os.path.join(HERMES_HOME, 'sessions/')
            # 找到今天的会话文件
            for f in glob.glob(f'{sessions_dir}{today}_*.jsonl'):
                try:
                    with open(f, 'r') as fp:
                        for line in fp:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                msg = json.loads(line)
                            except:
                                continue
                            if msg.get('role') == 'assistant':
                                tool_calls = msg.get('tool_calls', [])
                                for tc in tool_calls:
                                    name = tc.get('function', {}).get('name', '').lower()
                                    # 查询类工具
                                    if 'mem0_search' in name or 'mem0_profile' in name:
                                        today_queries += 1
                                    # 写入类工具
                                    elif 'mem0_conclude' in name or ('memory' in name and 'add' in name):
                                        today_writes += 1
                except:
                    pass
        except Exception as e:
            app.logger.error(f"Memory stats error: {e}")
        
        return jsonify({
            'status': status,
            'provider': provider,
            'count': count,
            'today_queries': today_queries,
            'today_writes': today_writes,
            'raw': output
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/hermes/model')
def hermes_model_info():
    """获取当前模型信息"""
    try:
        result = subprocess.run(['hermes', 'config'], capture_output=True, text=True, timeout=15, env=get_user_env())
        output = result.stdout
        
        model = 'unknown'
        provider = 'unknown'
        
        # 解析 Model 行 - 从 {'model': 'xxx', 'provider': 'xxx'} 提取
        model_match = re.search(r"'model':\s*'([^']*)'", output)
        if model_match:
            model = model_match.group(1)
        
        provider_match = re.search(r"'provider':\s*'([^']*)'", output)
        if provider_match:
            provider = provider_match.group(1)
        
        return jsonify({'model': model, 'provider': provider, 'raw': output})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/hermes/embedding')
def hermes_embedding_info():
    """获取向量引擎信息 (Ollama + embedding 模型)"""
    try:
        # 检查 Ollama 状态
        ollama_active = subprocess.run(
            ['pgrep', '-x', 'ollama'], capture_output=True, timeout=5
        ).returncode == 0
        
        models = []
        if ollama_active:
            result = subprocess.run(['ollama', 'list'], capture_output=True, text=True, timeout=10)
            for line in result.stdout.strip().split('\n')[1:]:  # 跳过表头
                parts = line.split()
                if len(parts) >= 3 and 'embedding' in parts[0].lower():
                    models.append({'name': parts[0], 'size': parts[2]})
        
        return jsonify({
            'engine': 'Ollama (本地)',
            'active': ollama_active,
            'models': models,
            'vector_db': 'Qdrant (本地)'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/hermes/alerts')
def hermes_alerts():
    """获取最近 24h 的 ERROR/WARN"""
    try:
        env = get_user_env()
        result = subprocess.run(
            ['journalctl', '--user', '-u', 'hermes-gateway', '--since', '24 hours ago', '--no-pager'],
            capture_output=True, text=True, timeout=15, env=env
        )
        lines = result.stdout.split('\n')
        alerts = []
        for line in lines:
            if 'ERROR' in line or 'WARNING' in line or 'CRITICAL' in line:
                # 简化时间戳
                alerts.append(line.strip())
        
        # 最近 20 条
        return jsonify({'alerts': alerts[-20:], 'total': len(alerts)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# === 命令执行 ===

ALLOWED_COMMANDS = {
    'hermes_status': ['hermes', 'status'],
    'gateway_status': ['hermes', 'gateway', 'status'],
    'gateway_restart': ['systemctl', '--user', 'restart', 'hermes-gateway'],
    'gateway_stop': ['systemctl', '--user', 'stop', 'hermes-gateway'],
    'gateway_start': ['systemctl', '--user', 'start', 'hermes-gateway'],
    'nginx_reload': ['sudo', 'systemctl', 'reload', 'nginx'],
    'nginx_restart': ['sudo', 'systemctl', 'restart', 'nginx'],
    'miniapp_restart': ['sudo', 'systemctl', 'restart', 'hermes-mini-app'],
    'ollama_restart': ['sudo', 'systemctl', 'restart', 'ollama'],
    'snell_restart': ['sudo', 'systemctl', 'restart', 'snell'],
    'journalctl_gateway': ['journalctl', '--user', '-u', 'hermes-gateway', '-n', '30', '--no-pager'],
    'df': ['df', '-h'],
    'free': ['free', '-h'],
    'uptime': ['uptime'],
    'hermes_backup': ['hermes', 'backup'],
    'hermes_doctor': ['hermes', 'doctor'],
    'hermes_update': ['hermes', 'update'],
}

USER_LEVEL_COMMANDS = {'gateway_restart', 'gateway_stop', 'journalctl_gateway'}

@app.route('/api/exec/<command>')
@require_token
def execute_command(command):
    if command not in ALLOWED_COMMANDS:
        return jsonify({'error': f'Unknown command: {command}'}), 400
    
    cmd = ALLOWED_COMMANDS[command]
    env = get_user_env()
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
        return jsonify({
            'command': ' '.join(cmd),
            'exit_code': result.returncode,
            'stdout': result.stdout,
            'stderr': result.stderr
        })
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Command timeout'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/health')
def health():
    return jsonify({'status': 'ok'})

@app.route('/api/ops/emergency-restart', methods=['POST'])
@require_ops_access
def emergency_restart():
    """紧急重启：杀掉所有 Hermes 相关进程，然后重启 Gateway"""
    env = get_user_env()
    results = []
    
    # 1. 停止 Gateway
    try:
        r = subprocess.run(['systemctl', '--user', 'stop', 'hermes-gateway'],
                           capture_output=True, text=True, timeout=10, env=env)
        results.append(f"stop gateway: exit={r.returncode}")
    except Exception as e:
        results.append(f"stop gateway: error={e}")
    
    # 2. 杀掉所有 hermes 相关进程
    try:
        # 找到所有 hermes 相关进程
        r = subprocess.run(['pgrep', '-f', 'hermes'], capture_output=True, text=True, timeout=5)
        pids = r.stdout.strip().split('\n') if r.stdout.strip() else []
        killed = []
        for pid in pids:
            pid = pid.strip()
            if pid and pid != str(os.getpid()):  # 不杀自己
                try:
                    os.kill(int(pid), 9)
                    killed.append(pid)
                except:
                    pass
        results.append(f"killed pids: {', '.join(killed) if killed else 'none'}")
    except Exception as e:
        results.append(f"kill processes: error={e}")
    
    # 3. 等一下再启动
    time.sleep(2)
    
    # 4. 启动 Gateway
    try:
        r = subprocess.run(['systemctl', '--user', 'start', 'hermes-gateway'],
                           capture_output=True, text=True, timeout=10, env=env)
        results.append(f"start gateway: exit={r.returncode}")
    except Exception as e:
        results.append(f"start gateway: error={e}")
    
    # 5. 验证状态
    time.sleep(1)
    try:
        r = subprocess.run(['systemctl', '--user', 'is-active', 'hermes-gateway'],
                           capture_output=True, text=True, timeout=5, env=env)
        active = r.stdout.strip() == 'active'
        results.append(f"gateway status: {'✅ running' if active else '❌ not running'}")
    except:
        results.append("gateway status: unknown")
    
    return jsonify({'results': results})

@app.route('/api/ops/restart-service', methods=['POST'])
@require_ops_access
def restart_service():
    """重启单个服务"""
    data = request.get_json() or {}
    service = data.get('service', '')
    
    env = get_user_env()
    
    # 服务名映射到实际命令
    service_map = {
        'gateway': (['systemctl', '--user', 'restart', 'hermes-gateway'], True),
        'nginx': (['sudo', 'systemctl', 'restart', 'nginx'], False),
        'miniapp': (['sudo', 'systemctl', 'restart', 'hermes-mini-app'], False),
        'ollama': (['sudo', 'systemctl', 'restart', 'ollama'], False),
        'snell': (['sudo', 'systemctl', 'restart', 'snell'], False),
    }
    
    if service not in service_map:
        return jsonify({'error': f'Unknown service: {service}'}), 400
    
    cmd, is_user = service_map[service]
    
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env if is_user else None)
        return jsonify({
            'service': service,
            'exit_code': r.returncode,
            'output': r.stdout + r.stderr
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/dashboard')
def dashboard():
    """一次性返回所有仪表盘数据，减少请求数"""
    try:
        # 并行获取
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        result = {'timestamp': int(time.time() * 1000)}
        
        # 系统数据
        cpu_per_core = psutil.cpu_percent(interval=None, percpu=True)
        cpu_percent = sum(cpu_per_core) / len(cpu_per_core)
        load = os.getloadavg()
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        uptime = int(time.time() - psutil.boot_time())
        io_stats = get_cached_io_stats()
        
        result['system'] = {
            'cpu': {'percent': cpu_percent, 'per_core': cpu_per_core, 'load1': float(load[0]), 'load5': float(load[1])},
            'memory': {'percent': mem.percent, 'used': mem.used, 'total': mem.total},
            'disk': {'percent': disk.percent, 'used': disk.used, 'total': disk.total},
            'uptime': uptime,
            'io': io_stats
        }
        
        # 进程数据
        procs = []
        for p in psutil.process_iter(['name', 'cpu_percent', 'memory_info']):
            try:
                cpu = p.info['cpu_percent'] or 0
                mem_bytes = p.info['memory_info'].rss if p.info['memory_info'] else 0
                procs.append({'name': p.info['name'], 'cpu': cpu, 'memory': mem_bytes})
            except:
                pass
        result['processes'] = {
            'cpu_top': sorted(procs, key=lambda x: x['cpu'], reverse=True)[:5],
            'mem_top': sorted(procs, key=lambda x: x['memory'], reverse=True)[:5]
        }
        
        # 服务状态
        services = ['gateway', 'hermes', 'ollama', 'snell']
        svc_result = []
        for svc in services:
            try:
                if svc == 'snell':
                    r = subprocess.run(['systemctl', 'is-active', 'snell'], capture_output=True, text=True, timeout=2)
                    active = r.stdout.strip() == 'active'
                elif svc == 'gateway':
                    r = subprocess.run(['systemctl', '--user', 'is-active', 'hermes-gateway'],
                                       capture_output=True, text=True, timeout=2, env=get_user_env())
                    active = r.stdout.strip() == 'active'
                elif svc == 'hermes':
                    r = subprocess.run(['hermes', 'status'], capture_output=True, text=True, timeout=10, env=get_user_env())
                    active = r.returncode == 0
                elif svc == 'ollama':
                    r = subprocess.run(['systemctl', 'is-active', 'ollama'], capture_output=True, text=True, timeout=2)
                    active = r.stdout.strip() == 'active'
                else:
                    active = False
                svc_result.append({'name': svc, 'status': 'running' if active else 'stopped'})
            except:
                svc_result.append({'name': svc, 'status': 'unknown'})
        result['services'] = svc_result
        
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=9120, debug=False)
