# Mode d'emploi:
# Description: Application backend Flask pour l'interface CaddyPanel.
# ... (reste des commentaires et imports initiaux) ...

import json
import os
import re 
import subprocess
from pathlib import Path
from functools import wraps
from flask import (Flask, render_template, url_for, request, jsonify, abort,
                   session, redirect, flash)
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta 

# --- Configuration ---
# ... (inchangé)
APP_DATA_DIR = Path(os.environ.get('APP_DATA_DIR', '.')).resolve() 
CADDY_CONFIG_FILE = Path(os.environ.get('CADDY_CONFIG', '/etc/caddy/Caddyfile')).resolve()
CADDY_ACCESS_LOG_FILE = Path(os.environ.get('CADDY_ACCESS_LOG_FILE', '/var/log/caddy_access.json.log'))

PREFERENCES_FILE = APP_DATA_DIR / 'preferences.json'
USERS_FILE = APP_DATA_DIR / 'users.json'
BASE_DIR = Path('.').resolve() 

DEFAULT_PREFERENCES = {
    "theme": "theme-light-gray",
    "caddyfilePath": str(CADDY_CONFIG_FILE), 
    "caddyReloadCmd": f"caddy reload --config {str(CADDY_CONFIG_FILE)} --adapter caddyfile",
    "globalAdminEmail": "", 
    "defaultAuthentikEnabled": False, 
    "defaultAuthentikOutpostUrl": "http://authentik.local:9000", 
    "defaultAuthentikUri": "/outpost.goauthentik.io/auth/caddy", 
    "defaultAuthentikCopyHeaders": "X-Authentik-Username X-Authentik-Groups X-Authentik-Email X-Authentik-Name X-Authentik-Uid X-Authentik-Jwt X-Authentik-Meta-Jwks", 
    "defaultAuthentikTrustedProxies": "private_ranges", 
    "defaultSkipTlsVerify": False 
}

app = Flask(__name__) 

app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'dev-only-unsafe-default-key-3f9a1z-CHANGE-IN-PROD')
app.config['USERS_FILE'] = USERS_FILE
app.config['PREFERENCES_FILE'] = PREFERENCES_FILE
app.config['CADDY_ACCESS_LOG_FILE'] = CADDY_ACCESS_LOG_FILE

# --- User Management Helpers ---
# ... (load_users, save_users, get_admin_user - inchangés)
def load_users():
    users_file_path = app.config['USERS_FILE']
    if not users_file_path.exists(): return {}
    try:
        with open(users_file_path, 'r') as f: return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"Error loading users file '{users_file_path}': {e}. Assuming no users.")
        return {}

def save_users(users):
    users_file_path = app.config['USERS_FILE']
    try:
        users_file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(users_file_path, 'w') as f: json.dump(users, f, indent=4)
        return True
    except IOError as e:
        print(f"Error saving users file '{users_file_path}': {e}")
        return False

def get_admin_user():
    users = load_users()
    return next(iter(users.values()), None) if users else None

# --- Preference Helpers ---
# ... (load_preferences, save_preferences - inchangés)
def load_preferences():
    prefs_file_path = app.config['PREFERENCES_FILE']
    if not prefs_file_path.exists():
        current_defaults = DEFAULT_PREFERENCES.copy()
        current_defaults["caddyfilePath"] = str(CADDY_CONFIG_FILE)
        return current_defaults
    try:
        with open(prefs_file_path, 'r') as f: prefs = json.load(f)
        for key, value in DEFAULT_PREFERENCES.items(): prefs.setdefault(key, value)
        prefs["caddyfilePath"] = str(CADDY_CONFIG_FILE)
        return prefs
    except (json.JSONDecodeError, IOError) as e:
        print(f"Error loading preferences file '{prefs_file_path}': {e}. Using defaults.")
        current_defaults = DEFAULT_PREFERENCES.copy()
        current_defaults["caddyfilePath"] = str(CADDY_CONFIG_FILE)
        return current_defaults

def save_preferences(prefs):
    prefs_file_path = app.config['PREFERENCES_FILE']
    try:
        prefs_file_path.parent.mkdir(parents=True, exist_ok=True)
        prefs["caddyfilePath"] = str(CADDY_CONFIG_FILE)
        with open(prefs_file_path, 'w') as f: json.dump(prefs, f, indent=4)
        return True
    except IOError as e:
        print(f"Error saving preferences file '{prefs_file_path}': {e}")
        return False

# --- Decorators ---
# ... (login_required, admin_setup_required - inchangés)
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            flash("Please log in to access this page.", "warning")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_setup_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if get_admin_user():
            if 'username' in session: return redirect(url_for('index'))
            else: return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# --- Routes ---
# ... (index, setup, login, logout, api/preferences, etc. - inchangés jusqu'à la partie stats)
@app.route('/')
@login_required
def index():
    # ... (code existant pour index - inchangé)
    prefs = load_preferences()
    caddyfile_to_load = CADDY_CONFIG_FILE
    initial_caddyfile_content = None
    error_message = None
    if caddyfile_to_load.exists():
        if caddyfile_to_load.is_file():
            try:
                initial_caddyfile_content = caddyfile_to_load.read_text(encoding='utf-8')
            except PermissionError: error_message = f"Error: Permission denied reading Caddyfile at '{caddyfile_to_load}'."
            except Exception as e: error_message = f"Error reading Caddyfile: {e}"
        else: error_message = f"Error: Configured Caddyfile path '{caddyfile_to_load}' is not a file."
    else: error_message = f"Warning: Caddyfile at '{caddyfile_to_load}' not found."
    if error_message and not initial_caddyfile_content: flash(error_message, "danger" if "Error:" in error_message else "info")
    return render_template('index.html', username=session.get('username'), initial_caddyfile_content=initial_caddyfile_content)

@app.route('/setup', methods=['GET', 'POST'])
@admin_setup_required
def setup():
    # ... (code existant pour setup - inchangé)
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        if not username or not password or not confirm_password: flash("All fields are required.", "danger")
        elif password != confirm_password: flash("Passwords do not match.", "danger")
        elif len(password) < 8: flash("Password must be at least 8 characters long.", "danger")
        else:
            users = {}
            hashed_password = generate_password_hash(password)
            users[username] = {'password': hashed_password}
            if save_users(users):
                flash("Admin account created successfully! Please log in.", "success")
                return redirect(url_for('login'))
            else: flash("Error saving admin account. Check server logs.", "danger")
        return render_template('setup.html')
    return render_template('setup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    # ... (code existant pour login - inchangé)
    if 'username' in session: return redirect(url_for('index'))
    if not get_admin_user():
         flash("No admin account found. Please set up the administrator.", "info")
         return redirect(url_for('setup'))
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        users = load_users()
        user_data = users.get(username)
        if user_data and check_password_hash(user_data['password'], password):
            session['username'] = username
            flash(f"Welcome back, {username}!", "success")
            return redirect(url_for('index'))
        else: flash("Invalid username or password.", "danger")
    return render_template('login.html')

@app.route('/logout')
def logout():
    # ... (code existant pour logout - inchangé)
    session.pop('username', None)
    flash("You have been logged out.", "info")
    return redirect(url_for('login'))

@app.route('/api/preferences', methods=['GET'])
@login_required
def get_preferences():
    # ... (code existant pour get_preferences - inchangé)
    prefs = load_preferences()
    return jsonify(prefs)

@app.route('/api/preferences', methods=['POST'])
@login_required
def post_preferences():
    # ... (code existant pour post_preferences - inchangé)
    try:
        new_prefs_input = request.get_json()
        if not isinstance(new_prefs_input, dict): return jsonify({"status": "error", "message": "Invalid data format"}), 400
        current_prefs = load_preferences()
        validated_prefs = {} 
        validation_errors = []
        for key, default_value in DEFAULT_PREFERENCES.items():
            if key in new_prefs_input:
                value = new_prefs_input[key]
                expected_type = type(default_value)
                if not isinstance(value, expected_type):
                    validation_errors.append(f"Invalid type for '{key}'")
                    validated_prefs[key] = current_prefs.get(key, default_value)
                    continue
                if key == "globalAdminEmail" and value and not re.match(r"[^@]+@[^@]+\.[^@]+", value): validation_errors.append(f"Invalid format for '{key}'")
                elif key == "caddyfilePath": value = str(CADDY_CONFIG_FILE)
                elif key == "caddyReloadCmd" and not value: validation_errors.append(f"Invalid value for '{key}': Cannot be empty.")
                if not any(err.startswith(f"Invalid type for '{key}'") or (f"Invalid format for '{key}'" in err) or (f"Invalid value for '{key}'" in err) for err in validation_errors):
                     validated_prefs[key] = value
                else: validated_prefs[key] = current_prefs.get(key, default_value)
            else: validated_prefs[key] = current_prefs.get(key, default_value)
        if validation_errors:
            if save_preferences(validated_prefs): return jsonify({"status": "warning", "message": "Preferences saved with errors.", "errors": validation_errors, "saved_prefs": validated_prefs}), 200
            else: return jsonify({"status": "error", "message": "Failed to save preferences with errors"}), 500
        if save_preferences(validated_prefs): return jsonify({"status": "success", "message": "Preferences saved", "saved_prefs": validated_prefs})
        else: return jsonify({"status": "error", "message": "Failed to save preferences"}), 500
    except json.JSONDecodeError: return jsonify({"status": "error", "message": "Invalid JSON"}), 400
    except Exception as e:
        print(f"Error in post_preferences: {e}")
        return jsonify({"status": "error", "message": "Internal server error"}), 500

@app.route('/api/browse', methods=['GET'])
@login_required
def browse_files():
    # ... (code existant pour browse_files - inchangé)
    req_path_str = request.args.get('path', '.')
    browse_base = BASE_DIR
    try:
        requested_path = browse_base.joinpath(req_path_str).resolve()
        if not str(requested_path).startswith(str(browse_base)): abort(403)
        if not requested_path.exists() or not requested_path.is_dir(): abort(404)
        items = []
        for item in requested_path.iterdir():
            try: items.append({"name": item.name, "path": item.relative_to(browse_base).as_posix(), "is_dir": item.is_dir()})
            except OSError: continue
        items.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
        parent_relative_path = None
        if requested_path != browse_base:
            parent_relative_path = requested_path.parent.relative_to(browse_base).as_posix()
            if parent_relative_path == '.': parent_relative_path = ''
        return jsonify({"current_path": requested_path.relative_to(browse_base).as_posix(), "parent_path": parent_relative_path, "items": items})
    except Exception as e: abort(500)

@app.route('/api/readfile', methods=['GET'])
@login_required
def read_file():
    # ... (code existant pour read_file - inchangé)
    req_path_str = request.args.get('path')
    if not req_path_str: return jsonify({"status": "error", "message": "No path"}), 400
    try:
        requested_file_path = BASE_DIR.joinpath(req_path_str).resolve()
        if not str(requested_file_path).startswith(str(BASE_DIR)): abort(403)
        if not requested_file_path.is_file(): return jsonify({"status": "error", "message": "Not a file"}), 404
        content = requested_file_path.read_text(encoding='utf-8')
        return jsonify({"status": "success", "path": req_path_str, "content": content})
    except FileNotFoundError: return jsonify({"status": "error", "message": "Not found"}), 404
    except PermissionError: return jsonify({"status": "error", "message": "Permission denied"}), 403
    except Exception as e: return jsonify({"status": "error", "message": f"Error: {e}"}), 500

@app.route('/api/caddyfile/save', methods=['POST'])
@login_required
def save_caddyfile_content():
    # ... (code existant pour save_caddyfile_content - inchangé, flash message retiré)
    try:
        data = request.get_json()
        content = data.get('content')
        if content is None: return jsonify({"status": "error", "message": "No content"}), 400
        CADDY_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True) 
        CADDY_CONFIG_FILE.write_text(content, encoding='utf-8')
        return jsonify({"status": "success", "message": f"Caddyfile saved to {CADDY_CONFIG_FILE}"})
    except PermissionError: return jsonify({"status": "error", "message": f"Permission denied writing to {CADDY_CONFIG_FILE}"}), 500
    except Exception as e: return jsonify({"status": "error", "message": f"Error: {e}"}), 500

@app.route('/api/caddy/reload', methods=['POST'])
@login_required
def reload_caddy_config():
    # ... (code existant pour reload_caddy_config - inchangé, flash message retiré)
    prefs = load_preferences()
    reload_cmd_str = prefs.get('caddyReloadCmd')
    if not reload_cmd_str: return jsonify({"status": "error", "message": "Reload command not configured."}), 400
    try:
        result = subprocess.run(reload_cmd_str, shell=True, capture_output=True, text=True, timeout=30, check=False)
        if result.returncode == 0:
            return jsonify({"status": "success", "message": "Caddy reloaded.", "output": result.stdout})
        else:
            error_detail = (result.stderr or result.stdout or "Unknown error.")[:500]
            return jsonify({"status": "error", "message": f"Reload failed. Code: {result.returncode}.", "details": error_detail}), 500
    except FileNotFoundError: return jsonify({"status": "error", "message": "Caddy command not found."}), 500
    except subprocess.TimeoutExpired: return jsonify({"status": "error", "message": "Reload command timed out."}), 500
    except Exception as e: return jsonify({"status": "error", "message": f"Error: {e}"}), 500

# --- Real Log Data Processing for Stats Page ---
# ... (read_caddy_logs, _process_logs_for_stats - inchangés)
def read_caddy_logs():
    log_file_path = app.config['CADDY_ACCESS_LOG_FILE']
    logs = []
    max_logs_to_process = 5000 
    if not log_file_path.exists():
        print(f"Log file not found: {log_file_path}")
        return logs
    try:
        lines_to_process = []
        with open(log_file_path, 'r', encoding='utf-8') as f:
            all_lines = f.readlines()
            lines_to_process = all_lines[-max_logs_to_process:]
        for line_str in lines_to_process:
            try:
                log_entry = json.loads(line_str)
                if 'ts' not in log_entry or not isinstance(log_entry['ts'], (float, int)):
                    continue
                log_entry.setdefault('request', {})
                log_entry['request'].setdefault('host', log_entry.get('host', 'Unknown'))
                log_entry['request'].setdefault('method', 'GET')
                log_entry['request'].setdefault('uri', '/')
                log_entry['request'].setdefault('headers', {}).setdefault('User-Agent', ['Unknown'])
                log_entry.setdefault('status', 0)
                log_entry.setdefault('size', 0)
                log_entry.setdefault('duration', 0)
                logs.append(log_entry)
            except json.JSONDecodeError as je: print(f"Error decoding JSON: {je} - Line: {line_str[:100]}")
            except Exception as e: print(f"Error processing log line: {e} - Log: {log_entry}")
        print(f"Read {len(logs)} log entries from {log_file_path}")
        return logs
    except IOError as e:
        print(f"Error reading log file {log_file_path}: {e}")
        return []
    except Exception as e:
        print(f"Unexpected error in read_caddy_logs: {e}")
        return []

def _process_logs_for_stats(logs):
    if not logs:
        return {"total_requests": 0, "requests_by_host": {}, "status_codes_dist": {}, "top_paths": [], "top_user_agents": [], "avg_response_time_ms": 0, "avg_response_size_kb": 0, "error_rate_percent": 0, "requests_timeseries": [], "data_from_utc": None, "data_to_utc": None, "log_read_error": None}
    total_requests = len(logs)
    requests_by_host = {}
    status_codes_dist = {"1xx":0, "2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0, "other": 0}
    path_counts = {}
    user_agent_counts = {}
    total_duration, total_size, error_count = 0, 0, 0
    min_ts_val = logs[0]["ts"]
    max_ts_val = logs[0]["ts"]
    requests_per_slot = {} 
    slot_duration_seconds = 10 * 60 

    for log in logs:
        try:
            current_ts = float(log.get("ts", 0))
            if current_ts == 0: continue
            min_ts_val = min(min_ts_val, current_ts)
            max_ts_val = max(max_ts_val, current_ts)
            host = log.get("request", {}).get("host", "UnknownHost")
            requests_by_host[host] = requests_by_host.get(host, 0) + 1
            status = int(log.get("status", 0))
            if 100 <= status <= 199: status_codes_dist["1xx"] += 1
            elif 200 <= status <= 299: status_codes_dist["2xx"] += 1
            elif 300 <= status <= 399: status_codes_dist["3xx"] += 1
            elif 400 <= status <= 499: status_codes_dist["4xx"] += 1; error_count +=1
            elif 500 <= status <= 599: status_codes_dist["5xx"] += 1; error_count +=1
            else: status_codes_dist["other"] += 1
            path = log.get("request", {}).get("uri", "UnknownURI").split("?")[0] 
            path_counts[path] = path_counts.get(path, 0) + 1
            ua_list = log.get("request", {}).get("headers", {}).get("User-Agent", ["UnknownUA"])
            ua_full = ua_list[0] if ua_list else "UnknownUA"
            ua_simple = ua_full.split('/')[0].split('(')[0].strip() or "UnknownUA"
            user_agent_counts[ua_simple] = user_agent_counts.get(ua_simple, 0) + 1
            total_duration += float(log.get("duration", 0))
            total_size += int(log.get("size", 0))
            time_slot_ts = int(current_ts / slot_duration_seconds) * slot_duration_seconds
            requests_per_slot[time_slot_ts] = requests_per_slot.get(time_slot_ts, 0) + 1
        except Exception as e:
            print(f"Error processing single log entry: {e} - Log: {log}")
            continue
    top_paths = [{"path": p, "count": c} for p, c in sorted(path_counts.items(), key=lambda item: item[1], reverse=True)[:10]]
    top_user_agents = [{"agent": ua, "count": c} for ua, c in sorted(user_agent_counts.items(), key=lambda item: item[1], reverse=True)[:5]]
    timeseries_data = []
    if requests_per_slot:
        sorted_slots = sorted(requests_per_slot.keys())
        current_slot_ts, end_slot_ts = sorted_slots[0], sorted_slots[-1]
        while current_slot_ts <= end_slot_ts:
            timeseries_data.append({"time": datetime.fromtimestamp(current_slot_ts).strftime('%Y-%m-%d %H:%M'), "count": requests_per_slot.get(current_slot_ts, 0)})
            current_slot_ts += slot_duration_seconds
    return {"total_requests": total_requests, "requests_by_host": dict(sorted(requests_by_host.items(), key=lambda item: item[1], reverse=True)[:7]), "status_codes_dist": status_codes_dist, "top_paths": top_paths, "top_user_agents": top_user_agents, "avg_response_time_ms": (total_duration / total_requests * 1000) if total_requests else 0, "avg_response_size_kb": (total_size / total_requests / 1024) if total_requests else 0, "error_rate_percent": (error_count / total_requests * 100) if total_requests else 0, "data_from_utc": datetime.fromtimestamp(min_ts_val).strftime('%Y-%m-%d %H:%M:%S UTC') if total_requests else "N/A", "data_to_utc": datetime.fromtimestamp(max_ts_val).strftime('%Y-%m-%d %H:%M:%S UTC') if total_requests else "N/A", "requests_timeseries": timeseries_data, "log_read_error": None}

@app.route('/stats')
@login_required
def stats_page():
    return render_template('stats.html', username=session.get('username'))

@app.route('/api/stats/global')
@login_required
def get_global_stats():
    log_read_error = None
    real_logs = []
    try:
        real_logs = read_caddy_logs()
        if not real_logs:
            if not app.config['CADDY_ACCESS_LOG_FILE'].exists():
                log_read_error = f"Log file {app.config['CADDY_ACCESS_LOG_FILE']} not found. Configure Caddy for JSON logging to stdout."
            else:
                log_read_error = f"No processable log entries in {app.config['CADDY_ACCESS_LOG_FILE']}. Check Caddy log format."
    except Exception as e:
        print(f"Critical error reading/processing logs: {e}")
        log_read_error = f"Server error processing logs: {e}"
    stats_data = _process_logs_for_stats(real_logs)
    if log_read_error: stats_data["log_read_error"] = log_read_error
    return jsonify(stats_data)

# --- Nouvelle API pour configurer le logging dans Caddyfile ---
@app.route('/api/caddyfile/configure_logging', methods=['POST'])
@login_required
def configure_caddyfile_logging():
    """
    Tente d'ajouter ou de modifier la configuration de log globale dans le Caddyfile
    pour utiliser JSON vers stdout.
    """
    caddyfile_path = CADDY_CONFIG_FILE
    desired_log_config = """
    log {
        output stdout
        format json {
            time_format rfc3339
        }
        level INFO
    }
"""
    try:
        if not caddyfile_path.exists():
            # Si le Caddyfile n'existe pas, on le crée avec le bloc global et le log
            # Cela pourrait être plus simple si l'entrypoint le crée toujours.
            # Pour l'instant, on assume qu'il est peu probable qu'il n'existe pas si l'app tourne.
            # Ou on crée un Caddyfile très basique.
            # On va plutôt retourner une erreur si le fichier n'existe pas, car l'utilisateur devrait
            # avoir au moins un Caddyfile de base via l'UI ou l'initialisation.
            return jsonify({"status": "error", "message": f"Caddyfile not found at {caddyfile_path}. Cannot configure logging."}), 500

        content = caddyfile_path.read_text(encoding='utf-8')
        new_content = ""

        # Regex pour trouver un bloc global existant { ... } au début du fichier
        # (simplifié, ne gère pas les commentaires complexes ou les blocs imbriqués au niveau global)
        global_block_match = re.match(r"^\s*\{([\s\S]*?)\}\s*", content, re.MULTILINE)

        if global_block_match:
            # Bloc global trouvé
            global_content = global_block_match.group(1)
            start_global_block = global_block_match.start(1) -1 # Inclure le '{'
            end_global_block = global_block_match.end(1) + 1 # Inclure le '}'
            
            # Vérifier si 'log {' est déjà dans le bloc global
            if re.search(r"^\s*log\s*\{", global_content, re.MULTILINE):
                # Remplacer le bloc log existant. C'est la partie la plus délicate.
                # Une approche simple est de supprimer l'ancien bloc log et d'ajouter le nouveau.
                # Attention : cela peut supprimer des configurations de log custom.
                # Pour cette version, on va être un peu direct :
                # On suppose que si l'utilisateur clique, il veut NOTRE config de log.
                
                # Enlever l'ancien bloc log (simpliste, pourrait être amélioré avec un meilleur parsing)
                # Ce regex tente de matcher 'log { ... }' en faisant attention aux accolades imbriquées simples.
                # Ce n'est PAS parfait pour des Caddyfiles très complexes.
                cleaned_global_content = re.sub(r"^\s*log\s*\{[\s\S]*?\}\s*$", "", global_content, flags=re.MULTILINE).strip()
                
                # Ajouter le nouveau bloc log au contenu global nettoyé
                if cleaned_global_content:
                    # S'il reste d'autres directives globales, ajouter le log avec une ligne vide avant/après pour la lisibilité
                    modified_global_content = f"{desired_log_config.strip()}\n\n{cleaned_global_content}" if cleaned_global_content.strip() else desired_log_config.strip()
                else:
                    modified_global_content = desired_log_config.strip()

                new_content = content[:start_global_block] + "{\n" + modified_global_content + "\n}" + content[end_global_block:]

            else:
                # Pas de bloc log, ajouter le nôtre au début du contenu du bloc global
                modified_global_content = f"{desired_log_config.strip()}\n{global_content.strip()}"
                new_content = content[:start_global_block] + "{\n" + modified_global_content + "\n}" + content[end_global_block:]
        else:
            # Pas de bloc global, en créer un au début du fichier avec notre log config
            new_content = "{\n" + desired_log_config.strip() + "\n}\n\n" + content

        # Sauvegarder le nouveau contenu
        caddyfile_path.write_text(new_content, encoding='utf-8')
        
        # Recharger Caddy (utilise la même logique que l'API /api/caddy/reload)
        prefs = load_preferences()
        reload_cmd_str = prefs.get('caddyReloadCmd')
        if not reload_cmd_str:
            return jsonify({"status": "warning", "message": "Caddyfile updated for logging, but reload command not configured. Please reload Caddy manually."}), 200

        result = subprocess.run(reload_cmd_str, shell=True, capture_output=True, text=True, timeout=30, check=False)
        if result.returncode == 0:
            return jsonify({"status": "success", "message": "Caddyfile updated for JSON logging and Caddy reloaded successfully."})
        else:
            error_detail = (result.stderr or result.stdout or "Unknown error during reload.")[:500]
            return jsonify({"status": "warning", 
                            "message": f"Caddyfile updated for JSON logging, but Caddy reload failed (Code: {result.returncode}). Check Caddy logs or Caddyfile syntax.",
                            "details": error_detail})

    except PermissionError:
        return jsonify({"status": "error", "message": f"Permission denied modifying Caddyfile at {caddyfile_path}"}), 500
    except Exception as e:
        print(f"Error configuring Caddyfile logging: {e}")
        return jsonify({"status": "error", "message": f"An unexpected error occurred: {e}"}), 500


# Point d'entrée pour exécuter l'application
if __name__ == '__main__':
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    CADDY_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not PREFERENCES_FILE.exists():
        print(f"Dev: Creating default preferences file at {PREFERENCES_FILE}")
        save_preferences(DEFAULT_PREFERENCES) 

    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('FLASK_PORT', 5000)))