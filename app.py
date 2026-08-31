import os, subprocess, signal, threading, time, json, shutil
from flask import Flask, render_template, jsonify, request, redirect, url_for, session, render_template_string, send_from_directory, Response, stream_with_context
from datetime import datetime, timedelta
import pytz
import re
import requests

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "SECURE_HOSTING_SECRET_KEY_123")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_ROOT = os.path.join(BASE_DIR, 'users')
DB_FILE = os.path.join(BASE_DIR, 'users.json')

# --- OWNER CREDENTIALS (CHANGE YOUR PASSWORD HERE OR SET ENVIRONMENT VARIABLE) ---
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "owner")
OWNER_PASSWORD = os.getenv("OWNER_PASSWORD", "admin12345") # <-- Aapka personal owner password

OPENROUTER_API_KEY = "sk-or-v1-REMOVED"
AI_MODEL = "meta-llama/llama-3-8b-instruct"
API_URL = "https://openrouter.ai/api/v1/chat/completions"

# --- LOCAL DATABASE LOGIC (LOCAL ONLY, NO GITHUB SYNC) ---
def load_users():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"Local Load Error: {e}")
    
    default_users = {
        OWNER_USERNAME: {
            "p": OWNER_PASSWORD,
            "disk": 2048,
            "memory": "2GB",
            "status": "active",
            "role": "owner",
            "created_at": datetime.now().strftime('%d-%m-%Y %H:%M:%S'),
            "expiry_date": "Lifetime"
        }
    }
    save_users(default_users)
    return default_users

def save_users(data):
    with open(DB_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4)

if not os.path.exists(USERS_ROOT): 
    os.makedirs(USERS_ROOT)

# --- HELPER FUNCTIONS ---
def get_user_path():
    if 'username' not in session: return USERS_ROOT
    path = os.path.join(USERS_ROOT, session['username'])
    if not os.path.exists(path): os.makedirs(path)
    return path

def get_venv_path():
    path = os.path.join(get_user_path(), 'lib_env')
    if not os.path.exists(path): os.makedirs(path)
    return path

def get_dir_size(path):
    total = 0
    try:
        if not os.path.exists(path):
            return 0
        for dirpath, dirnames, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if not os.path.islink(fp):
                    total += os.path.getsize(fp)
    except Exception as e:
        print(f"Size Error: {e}")
        return 0
    return round(total / (1024 * 1024), 2)

# --- GLOBAL STORAGE & PROCESS TRACKING ---
console_logs = {"terminal": "Terminal Ready...\n"}
running_processes = {}
file_start_times = {}
user_activities = {}

def log_activity(action, details):
    if 'username' not in session: return
    u = session['username']
    if u not in user_activities:
        user_activities[u] = []
    
    entry = {
        "action": action,
        "details": details,
        "time": time.strftime("%I:%M %p"),
        "ip": request.remote_addr
    }
    user_activities[u].insert(0, entry)
    user_activities[u] = user_activities[u][:20]

def capture(fk, fn, p, log_file_path):
    try:
        with open(log_file_path, "a", encoding="utf-8") as f:
            start_time = time.strftime('%H:%M:%S')
            f.write(f'<div style="color: #2ecc71;">[{start_time}] {fn} starting...</div>\n')
            f.flush()
            
            for line in iter(p.stdout.readline, b''):
                decoded_line = line.decode('utf-8', errors='ignore')
                f.write(f'<div style="color: #ffffff; background: #1e1e1e; font-family: monospace;">{decoded_line}</div>')
                f.flush()
                
            p.stdout.close()
            stop_time = time.strftime('%H:%M:%S')
            f.write(f'<div style="color: #e74c3c;">[{stop_time}] Process stopped.</div>\n')
            f.flush()
    except Exception as e:
        print(f"Logging Error: {e}")
    finally:
        if fk in running_processes: del running_processes[fk]
        if fk in file_start_times: del file_start_times[fk]

@app.route('/run/<path:filename>')
def start_file(filename):
    if 'username' not in session: return jsonify({"status":"error", "msg":"Login first"})
    
    user = session['username']
    users = load_users()
    u = users.get(user, {})
    
    user_dir = get_user_path()
    current_usage_mb = get_dir_size(user_dir)
    mem_limit_str = str(u.get('memory', '512MB')).upper()
    
    limit_in_mb = 0
    match = re.match(r"(\d+)", mem_limit_str)
    if match:
        val = float(match.group(1))
        if 'GB' in mem_limit_str: limit_in_mb = val * 1024
        elif 'KB' in mem_limit_str: limit_in_mb = val / 1024
        else: limit_in_mb = val
    else:
        limit_in_mb = u.get('disk', 1024)

    if current_usage_mb >= limit_in_mb:
        return jsonify({"status": "error", "msg": f"Storage Full!"})

    file_key = f"{user}_{filename}"
    file_path = os.path.join(user_dir, filename)
    log_file_path = os.path.join(user_dir, f"{filename}.log")

    if not os.path.exists(file_path):
        return jsonify({"status":"error", "msg":"File not found!"})

    if file_key in running_processes:
        try: 
            pid = running_processes[file_key]
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except:
                os.kill(pid, signal.SIGKILL)
            time.sleep(1)
        except: pass
        finally:
            running_processes.pop(file_key, None)

    env = os.environ.copy()
    env['PYTHONPATH'] = get_venv_path() + os.pathsep + env.get('PYTHONPATH', '')
    env['PYTHONUNBUFFERED'] = '1'
    
    try:
        log_activity("server:run", f"Started: {filename}")
        proc = subprocess.Popen(
            ['python3', '-u', file_path], 
            stdout=subprocess.PIPE, 
            stderr=subprocess.STDOUT, 
            env=env,
            cwd=user_dir,
            start_new_session=True 
        )
        
        running_processes[file_key] = proc.pid
        file_start_times[file_key] = time.time()
        threading.Thread(target=capture, args=(file_key, filename, proc, log_file_path), daemon=True).start()
        
        return jsonify({"status":"success", "msg": f"{filename} Running 24/7..."})
    except Exception as e:
        return jsonify({"status":"error", "msg": f"Launch Error: {str(e)}"})

@app.route('/stop/<path:filename>')
def stop_file(filename):
    if 'username' not in session: return jsonify({"status":"error"})
    
    user = session.get('username')
    file_key = f"{user}_{filename}"
    user_dir = get_user_path()

    log_activity("server:stop", f"Stopped process: {filename}")
    log_file_path = os.path.join(user_dir, f"{filename}.log")
    
    if file_key in running_processes:
        try:
            os.kill(running_processes[file_key], signal.SIGKILL)
            del running_processes[file_key]
            if file_key in file_start_times: del file_start_times[file_key]
        except:
            if file_key in running_processes: del running_processes[file_key]

    try:
        if os.path.exists(log_file_path):
            os.remove(log_file_path)
        if file_key in console_logs:
            console_logs[file_key] = "" 
    except:
        pass

    return jsonify({"status":"success", "msg":"Bot Stopped Successfully!"})

@app.route('/restart/<path:filename>')
def restart_file(filename):
    if 'username' not in session: 
        return jsonify({"status":"error", "msg":"Login first"})
    
    user = session['username']
    users = load_users()
    u = users.get(user, {})
    
    user_dir = get_user_path()
    current_usage_mb = get_dir_size(user_dir)
    
    mem_limit_str = str(u.get('memory', '512MB')).upper()
    limit_in_mb = 0
    match = re.match(r"(\d+)", mem_limit_str)
    
    if match:
        val = float(match.group(1))
        if 'GB' in mem_limit_str: limit_in_mb = val * 1024
        elif 'KB' in mem_limit_str: limit_in_mb = val / 1024
        else: limit_in_mb = val 
    else:
        limit_in_mb = u.get('disk', 1024)

    if current_usage_mb >= limit_in_mb:
        return jsonify({
            "status": "error", 
            "msg": f"Storage Full! ({current_usage_mb}MB / {mem_limit_str}). Delete files to restart!"
        })

    file_key = f"{user}_{filename}"
    
    if file_key in running_processes:
        pid = running_processes[file_key]
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except:
            try:
                os.kill(pid, signal.SIGKILL)
            except:
                pass
        
        running_processes.pop(file_key, None)
        if file_key in file_start_times:
            del file_start_times[file_key]

    log_activity("server:restart", f"Restarting: {filename}")
    time.sleep(1.5) 
    return start_file(filename)

last_net_stats = {"in": 0, "out": 0, "time": time.time()}

@app.route('/stats')
def stats():
    global last_net_stats
    if 'username' not in session: 
        return jsonify({"status":"error"}), 401
    
    users = load_users()
    user = session.get('username')
    u = users.get(user, {})
    
    user_dir = get_user_path()
    user_storage_usage = get_dir_size(user_dir)

    try:
        load1, load5, load15 = os.getloadavg()
        cpu_count = os.cpu_count() or 1
        cpu_usage = round((load1 / cpu_count) * 100, 1)
        if cpu_usage > 100: cpu_usage = 99.9
    except:
        cpu_usage = "32.5"

    kolkata_tz = pytz.timezone('Asia/Kolkata')
    current_time_str = datetime.now(kolkata_tz).strftime('%d-%m-%Y %H:%M:%S')
    expiry_date = u.get('expiry_date', 'N/A')

    filename = request.args.get('file', 'main.py')
    file_key = f"{user}_{filename}"
    uptime_str = "Offline"
    
    if file_key in running_processes:
        pid = running_processes[file_key]
        is_alive = False
        try:
            os.kill(pid, 0) 
            is_alive = True
        except (OSError, ProcessLookupError):
            is_alive = False

        if is_alive and file_key in file_start_times:
            elapsed = int(time.time() - file_start_times[file_key])
            h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
            uptime_str = f"{h}h {m}m {s}s"
        else:
            running_processes.pop(file_key, None)
            file_start_times.pop(file_key, None)
            uptime_str = "Offline"

    net_in_str, net_out_str = "0.00 MiB", "0.00 MiB"
    try:
        with open("/proc/net/dev", "r") as f:
            lines = f.readlines()
            for line in lines:
                if any(x in line for x in ["wlan0", "eth0", "rmnet", "enp", "venet"]):
                    data = line.split()
                    curr_in, curr_out = int(data[1]), int(data[9])
                    if last_net_stats.get("in", 0) > 0:
                        diff_in = (curr_in - last_net_stats["in"]) / (1024 * 1024)
                        diff_out = (curr_out - last_net_stats["out"]) / (1024 * 1024)
                        net_in_str = f"{diff_in:.2f} MiB"
                        net_out_str = f"{diff_out:.2f} MiB"
                    last_net_stats["in"], last_net_stats["out"] = curr_in, curr_out
                    break
    except:
        net_in_str, net_out_str = "0.12 MiB", "0.05 MiB"

    return jsonify({
        "cpu": f"{cpu_usage}%", 
        "ram": f"{user_storage_usage}MB / {u.get('memory', '6GB')}",
        "disk": f"{user_storage_usage}MB / {u.get('disk', 1024)}MB",
        "uptime": uptime_str,
        "net_in": net_in_str,
        "net_out": net_out_str,
        "current_time": current_time_str,
        "expiry_date": expiry_date
    })

@app.route('/command', methods=['POST'])
def terminal():
    if 'username' not in session: 
        return jsonify({"status":"error", "msg": "Unauthorized"}), 401
        
    user = session['username']
    users = load_users()
    u = users.get(user, {})
    
    user_dir = get_user_path() 
    current_usage_mb = get_dir_size(user_dir)
    
    mem_limit_str = str(u.get('memory', '512MB')).upper()
    limit_in_mb = 0
    match = re.match(r"(\d+)", mem_limit_str)
    if match:
        val = float(match.group(1))
        if 'GB' in mem_limit_str: limit_in_mb = val * 1024
        elif 'KB' in mem_limit_str: limit_in_mb = val / 1024
        else: limit_in_mb = val 
    else:
        limit_in_mb = u.get('disk', 1024)

    if current_usage_mb >= limit_in_mb:
        return jsonify({
            "status": "error", 
            "msg": f"Storage Full! ({current_usage_mb}MB / {mem_limit_str}). Delete files first."
        })

    data = request.json
    cmd = data.get('cmd', '').strip()
    log_key = f"{user}_terminal"
    target_lib_dir = os.path.join(user_dir, 'lib_env') 

    if not cmd: 
        return jsonify({"status":"error", "msg": "Empty command"})

    console_logs[log_key] = f'<span style="color: #ffffff;">[{time.strftime("%H:%M:%S")}] {user}:~$ {cmd}</span>\n'

    def run_process(full_cmd, is_uninstall=False, pkg_name=None, is_install=False):
        try:
            env = os.environ.copy()
            env["PYTHONPATH"] = f"{target_lib_dir}:{env.get('PYTHONPATH', '')}"
            env["PYTHONUNBUFFERED"] = "1" 

            if is_uninstall and pkg_name:
                console_logs[log_key] += f'<span style="color: #ff4444;">[{time.strftime("%H:%M:%S")}] Removing {pkg_name}...</span>\n'
                deleted_items = []
                if os.path.exists(target_lib_dir):
                    clean_name = pkg_name.replace('-', '_').lower()
                    for item in os.listdir(target_lib_dir):
                        item_lower = item.lower()
                        if item_lower.startswith(clean_name) or item_lower.startswith(pkg_name.lower()):
                            item_path = os.path.join(target_lib_dir, item)
                            try:
                                if os.path.isdir(item_path): shutil.rmtree(item_path)
                                else: os.remove(item_path)
                                deleted_items.append(item)
                                console_logs[log_key] += f'<span style="color: #00d4ff;">Removed: {item}</span>\n'
                            except Exception as e:
                                console_logs[log_key] += f'<span style="color: #ffcc00;">Error deleting {item}</span>\n'
                
                if deleted_items:
                    console_logs[log_key] += f'<span style="color: #50fa7b;">Successfully uninstalled {pkg_name}.</span>\n'
                return

            proc = subprocess.Popen(
                full_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=user_dir,
                env=env,
                bufsize=1,
                universal_newlines=True,
                start_new_session=True
            )
            
            for line in proc.stdout:
                console_logs[log_key] += line
                
            proc.wait()

            if is_install and proc.returncode == 0:
                console_logs[log_key] += f'</span>\n<span style="color: #00ff88; font-weight: bold;">Successfully installed {pkg_name}.</span>'

            console_logs[log_key] += f'\n<span style="color: #ff4444;">[{time.strftime("%H:%M:%S")}] Process Exited (Code: {proc.returncode})</span>\n'
            
        except Exception as e:
            console_logs[log_key] += f'\n<span style="color: #ff3333; font-family: monospace;">[SYSTEM ERROR]: {str(e)}</span>\n'

    if cmd.startswith(('pip install ', 'pkg install ')):
        pkg = cmd.split('install ')[1].strip()
        if not os.path.exists(target_lib_dir): os.makedirs(target_lib_dir)
        target_cmd = ['pip', 'install', pkg, '--no-cache-dir', '--no-user', '--target', target_lib_dir]
        threading.Thread(target=run_process, args=(target_cmd, False, pkg, True), daemon=True).start()
        return jsonify({"status":"success", "msg":f"Installing {pkg}..."})

    elif cmd.startswith('pip uninstall '):
        pkg = cmd.split('uninstall ')[1].strip()
        threading.Thread(target=run_process, args=(None, True, pkg), daemon=True).start()
        return jsonify({"status":"success", "msg":f"Uninstalling {pkg}..."})

    elif cmd.startswith(('python3 ', 'python ')):
        parts = cmd.split(' ')
        target_file = parts[1] if len(parts) > 1 else ""
        target_cmd = ['python3', '-u', target_file]
        threading.Thread(target=run_process, args=(target_cmd,), daemon=True).start()
        return jsonify({"status":"success", "msg":f"Running {target_file}..."})

    else:
        threading.Thread(target=run_process, args=(cmd.split(' '),), daemon=True).start()
        return jsonify({"status":"success", "msg":"Executing command..."})

@app.route('/create_file', methods=['POST'])
def create_file():
    if 'username' not in session: 
        return jsonify({"status": "error", "msg": "Login first"}), 401

    user = session.get('username')
    users = load_users()
    u = users.get(user, {})
    
    user_dir = get_user_path()
    current_usage_mb = get_dir_size(user_dir)
    
    mem_limit_str = str(u.get('memory', '512MB')).upper()
    limit_in_mb = 0
    match = re.match(r"(\d+)", mem_limit_str)
    
    if match:
        val = float(match.group(1))
        if 'GB' in mem_limit_str: limit_in_mb = val * 1024
        elif 'KB' in mem_limit_str: limit_in_mb = val / 1024
        else: limit_in_mb = val 
    else:
        limit_in_mb = u.get('disk', 1024)

    if current_usage_mb >= limit_in_mb:
        return jsonify({
            "status": "error", 
            "msg": f"Storage Full! ({current_usage_mb}MB / {mem_limit_str}). Delete some files!"
        })

    data = request.json
    filename = data.get('name')
    
    if not filename:
        return jsonify({"status": "error", "msg": "File name is required!"})

    log_activity("file:create", f"Created new file: {filename}")
    path = os.path.join(user_dir, data.get('path', ''), filename)
    
    try:
        with open(path, 'w', encoding='utf-8') as f: 
            f.write("")
        return jsonify({"status": "success", "msg": f"'{filename}' Created!"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})

@app.route('/create_folder', methods=['POST'])
def create_folder():
    data = request.json
    foldername = data.get('name')
    log_activity("file:create_folder", f"Created folder: {foldername}")
    path = os.path.join(get_user_path(), data.get('path', ''), foldername)
    if not os.path.exists(path): os.makedirs(path)
    return jsonify({"status":"success"})

@app.route('/edit/<path:name>', methods=['GET', 'POST'])
def web_edit_file(name):
    path = os.path.join(get_user_path(), name)
    user = session['username']
    
    if request.method == 'POST':
        log_activity("file:edit", f"Updated file content: {name}")
        content = request.json.get('content')
        try:
            with open(path, 'w', encoding='utf-8') as f: 
                f.write(content)
            msg = f'<b style="color: #2ecc71; font-size: 25px;">{name.upper()} Update Done</b>'
            return jsonify({"status": "success", "msg": msg})
        except Exception as e:
            msg = f'<b style="color: #e74c3c; font-size: 25px;">ERROR: {str(e).upper()}</b>'
            return jsonify({"status": "error", "msg": msg})
    
    with open(path, 'r', encoding='utf-8') as f: 
        return jsonify({"content": f.read()})

@app.route('/delete/<path:name>')
def delete_item(name):
    log_activity("file:delete", f"Permanently deleted: {name}")
    path = os.path.join(get_user_path(), name)
    
    if os.path.isfile(path):
        os.remove(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)

    return jsonify({"status":"success"})

@app.route('/rename', methods=['POST'])
def rename_item():
    data = request.json
    old_name = data.get('old')
    new_name = data.get('new')
    
    log_activity("file:rename", f"Renamed {old_name} to {new_name}")
    old = os.path.join(get_user_path(), old_name)
    new = os.path.join(get_user_path(), new_name)
    os.rename(old, new)
    return jsonify({"status":"success"})

@app.route('/logs/<path:filename>')
def get_logs(filename):
    if 'username' not in session: return jsonify({"logs": ""})
    
    user = session.get('username')
    user_dir = os.path.join(USERS_ROOT, user)
    
    if filename == "terminal":
        log_key = f"{user}_terminal"
        return jsonify({"logs": console_logs.get(log_key, "System Ready...\n")})
    
    log_file_path = os.path.join(user_dir, f"{filename}.log")
    if os.path.exists(log_file_path):
        try:
            with open(log_file_path, "r", encoding="utf-8") as f:
                content = f.read()
                return jsonify({"logs": content[-20000:] if len(content) > 20000 else content})
        except:
            return jsonify({"logs": "Error 404!"})
    
    return jsonify({"logs": "Main.py Not Running."})

@app.route('/')
def index():
    if 'username' not in session: return redirect(url_for('login'))
    
    user_dir = get_user_path()
    rel_path = request.args.get('path', '')
    target_path = os.path.abspath(os.path.join(user_dir, rel_path))
    files = []
    
    if os.path.exists(target_path):
        for entry in os.scandir(target_path):
            files.append({
                "name": entry.name, "is_dir": entry.is_dir(),
                "rel_path": os.path.relpath(entry.path, user_dir)
            })
    files = sorted(files, key=lambda x: (not x['is_dir'], x['name']))
    return render_template('index.html', files=files, user=session['username'], current_path=rel_path)

app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

# --- LOGIN (OWNER LOGIN ONLY) ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        try:
            data = request.get_json() or {}
            u_input = data.get('username', '').strip()
            p_input = data.get('password', '').strip()

            if not u_input or not p_input:
                return jsonify({"status": "error", "msg": "Empty fields!"}), 400

            users = load_users()
            user_data = users.get(u_input)

            if user_data and str(user_data.get('p')) == str(p_input):
                session.clear()
                session.permanent = True
                session['username'] = u_input
                return jsonify({"status": "success", "msg": "Owner Login Successful! Welcome Owner."})
            else:
                return jsonify({"status": "error", "msg": "Invalid Username or Password!"}), 401
                
        except Exception as e:
            return jsonify({"status": "error", "msg": f"Server Error: {str(e)}"}), 500
        
    return render_template('login.html')

@app.route('/register', methods=['POST'])
def register():
    return jsonify({'status': 'error', 'msg': 'Registration is disabled. Only Owner can login.'}), 403

@app.route('/logout')
def logout():
    if 'username' in session:
        log_activity("server:logout", "Owner logged out.")
    session.clear()
    return redirect(url_for('login'))

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'username' not in session: 
        return jsonify({"status": "error", "msg": "Login first"}), 401
    
    user = session['username']
    users = load_users()
    u = users.get(user, {})
    
    user_dir = get_user_path()
    current_usage_mb = get_dir_size(user_dir)
    
    mem_limit_str = str(u.get('memory', '512MB')).upper()
    limit_in_mb = 0
    match = re.match(r"(\d+)", mem_limit_str)
    if match:
        val = float(match.group(1))
        if 'GB' in mem_limit_str: limit_in_mb = val * 1024
        elif 'KB' in mem_limit_str: limit_in_mb = val / 1024
        else: limit_in_mb = val
    else:
        limit_in_mb = u.get('disk', 1024) 

    if current_usage_mb >= limit_in_mb:
        return jsonify({
            "status": "error", 
            "msg": f"Storage Full! ({current_usage_mb}MB / {mem_limit_str}). Delete some files."
        }), 403

    if 'file' not in request.files:
        return jsonify({"status": "error", "msg": "No file part!"})
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"status": "error", "msg": "No selected file!"})

    if file:
        file.seek(0, os.SEEK_END)
        file_length = file.tell() / (1024 * 1024)
        file.seek(0)

        if (current_usage_mb + file_length) > limit_in_mb:
            return jsonify({
                "status": "error", 
                "msg": f"Upload Failed! File ({round(file_length, 2)}MB) exceeds storage limit."
            }), 403

        log_activity("file:upload", f"Uploaded: {file.filename}")
        rel_path = request.form.get('path', '')
        target_dir = os.path.join(user_dir, rel_path)
        
        if not os.path.exists(target_dir):
            os.makedirs(target_dir)
            
        file_path = os.path.join(target_dir, file.filename)
        file.save(file_path)
        return jsonify({"status": "success", "msg": f"'{file.filename}' Uploaded Successfully!"})

@app.route('/activity.html')
def activity_log():
    if 'username' not in session: 
        return '<p class="p-5 text-red-500">Session expired. Please login.</p>', 401
    
    user = session['username']
    acts = user_activities.get(user, [])
    return render_template('activity.html', activities=acts, user=user)

@app.route('/download/<path:filename>')
def download_file(filename):
    if 'username' not in session:
        return "Unauthorized! Login first", 401
    
    user_directory = os.path.abspath(get_user_path())
    safe_filename = filename.lstrip('/')
    file_full_path = os.path.join(user_directory, safe_filename)
    
    if os.path.exists(file_full_path) and os.path.isfile(file_full_path):
        return send_from_directory(user_directory, safe_filename, as_attachment=True)
    else:
        return "File Not Found!", 404

@app.route('/terms')
def terms():
    return render_template('terms.html')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

# --- 24/7 AUTO RESTART BOTS ON SERVER STARTUP (LOCAL ONLY, NO GITHUB SENDER) ---
def restart_all_active_bots():
    print("[24/7 SYSTEM] Auto-restarting bots locally...")
    users = load_users()
    if not users: return

    for username, data in users.items():
        if str(data.get('status', '')).lower() == 'active':
            user_dir = os.path.join(USERS_ROOT, username)
            main_file = os.path.join(user_dir, 'main.py')
            
            if os.path.exists(main_file):
                file_key = f"{username}_main.py"
                log_file_path = os.path.join(user_dir, "main.py.log")
                
                env = os.environ.copy()
                venv_path = os.path.join(user_dir, 'lib_env')
                env['PYTHONPATH'] = venv_path + os.pathsep + env.get('PYTHONPATH', '')
                env['PYTHONUNBUFFERED'] = '1'

                try:
                    if file_key in running_processes:
                        try:
                            old_pid = running_processes[file_key]
                            os.kill(old_pid, signal.SIGKILL)
                        except: pass
                        running_processes.pop(file_key, None)

                    # Start process in independent session so it stays alive 24/7
                    proc = subprocess.Popen(
                        ['python3', '-u', main_file], 
                        stdout=subprocess.PIPE, 
                        stderr=subprocess.STDOUT, 
                        env=env,
                        cwd=user_dir,
                        start_new_session=True 
                    )
                    
                    running_processes[file_key] = proc.pid
                    file_start_times[file_key] = time.time()
                    threading.Thread(target=capture, args=(file_key, "main.py", proc, log_file_path), daemon=True).start()
                    print(f"✅ [24/7 RESTARTED] {username}/main.py (PID: {proc.pid})")
                except Exception as e:
                    print(f"❌ [24/7 ERROR] {username}: {e}")

if __name__ == '__main__':
    if not os.path.exists(USERS_ROOT): os.makedirs(USERS_ROOT)
    load_users()
    
    # Start 24/7 bots in background local thread on server startup
    threading.Thread(target=restart_all_active_bots, daemon=True).start()
    
    port = int(os.environ.get("PORT", 15029))
    print(f"Starting server on port {port}... Single Owner access & 24/7 Bot execution active.")
    app.run(host='0.0.0.0', port=port)
