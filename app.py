# Usage:
# Description: Flask backend application for the CaddyPanel interface.
# ... (rest of initial comments and imports) ...

import json
import os
import re 
import subprocess
from pathlib import Path
from functools import wraps
from flask import (Flask, render_template, url_for, request, jsonify, abort,
                   session, redirect, flash)
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta, timezone
import secrets
import stats_aggregator

# --- Configuration ---
# ... (unchanged)
APP_DATA_DIR = Path(os.environ.get('APP_DATA_DIR', '.')).resolve() 
CADDY_CONFIG_FILE = Path(os.environ.get('CADDY_CONFIG', os.environ.get('CADDY_CONFIG_FILE', '/etc/caddy/Caddyfile'))).resolve()
CADDY_ACCESS_LOG_FILE = Path(os.environ.get('CADDY_ACCESS_LOG_FILE', '/var/log/caddy_access.json.log'))

STATS_DB_PATH = APP_DATA_DIR / 'stats.db'
PREFERENCES_FILE = APP_DATA_DIR / 'preferences.json'
USERS_FILE = APP_DATA_DIR / 'users.json'
BASE_DIR = Path('.').resolve() 

DEFAULT_PREFERENCES = {
    "theme": "theme-light-gray",
    "caddyfilePath": str(CADDY_CONFIG_FILE), 
    "globalAdminEmail": "", 
    "defaultAuthentikEnabled": False, 
    "defaultAuthentikOutpostUrl": "http://authentik.local:9000", 
    "defaultAuthentikUri": "/outpost.goauthentik.io/auth/caddy", 
    "defaultAuthentikCopyHeaders": "X-Authentik-Username X-Authentik-Groups X-Authentik-Email X-Authentik-Name X-Authentik-Uid X-Authentik-Jwt X-Authentik-Meta-Jwks", 
    "defaultAuthentikTrustedProxies": "private_ranges", 
    "defaultSkipTlsVerify": False 
}

app = Flask(__name__) 

_flask_secret = os.environ.get('FLASK_SECRET_KEY')
if not _flask_secret or _flask_secret == 'dev-only-unsafe-default-key-3f9a1z-CHANGE-IN-PROD':
    app.config['SECRET_KEY'] = secrets.token_hex(32)
    print("WARNING: FLASK_SECRET_KEY not set or using the default insecure key. A random temporary key was generated. "
          "Sessions will not persist across restarts. Set FLASK_SECRET_KEY environment variable for production use.")
else:
    app.config['SECRET_KEY'] = _flask_secret
app.config['USERS_FILE'] = USERS_FILE
app.config['PREFERENCES_FILE'] = PREFERENCES_FILE
app.config['CADDY_ACCESS_LOG_FILE'] = CADDY_ACCESS_LOG_FILE
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Initialize stats database (works both with dev server and gunicorn)
stats_aggregator.init_stats_db(STATS_DB_PATH)

# --- User Management Helpers ---
# ... (load_users, save_users, get_admin_user - unchanged)
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
# ... (load_preferences, save_preferences - unchanged)
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
# ... (login_required, admin_setup_required - unchanged)
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
# ... (index, setup, login, logout, api/preferences, etc. - unchanged until the stats part)
@app.route('/')
@login_required
def index():
    # ... (existing code for index - unchanged)
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
    # ... (existing code for setup - unchanged)
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        if not username or not password or not confirm_password: flash("All fields are required.", "danger")
        elif password != confirm_password: flash("Passwords do not match.", "danger")
        elif len(password) < 8: flash("Password must be at least 8 characters long.", "danger")
        else:
            if get_admin_user():
                flash("An admin account already exists.", "danger")
                return redirect(url_for('login'))
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
    # ... (existing code for login - unchanged)
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
    # ... (existing code for logout - unchanged)
    session.pop('username', None)
    flash("You have been logged out.", "info")
    return redirect(url_for('login'))

@app.route('/api/preferences', methods=['GET'])
@login_required
def get_preferences():
    # ... (existing code for get_preferences - unchanged)
    prefs = load_preferences()
    return jsonify(prefs)

@app.route('/api/preferences', methods=['POST'])
@login_required
def post_preferences():
    # ... (existing code for post_preferences - unchanged)
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
                if not any(err.startswith(f"Invalid type for '{key}'") or (f"Invalid format for '{key}'" in err) for err in validation_errors):
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
    # ... (existing code for browse_files - unchanged)
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
    # ... (existing code for read_file - unchanged)
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
    # ... (existing code for save_caddyfile_content - unchanged, flash message removed)
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
    # ... (existing code for reload_caddy_config - unchanged, flash message removed)
    try:
        command = ["caddy", "reload", "--config", str(CADDY_CONFIG_FILE), "--adapter", "caddyfile"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
        if result.returncode == 0:
            return jsonify({"status": "success", "message": "Caddy reloaded.", "output": result.stdout})
        else:
            error_detail = (result.stderr or result.stdout or "Unknown error.")[:500]
            return jsonify({"status": "error", "message": f"Reload failed. Code: {result.returncode}.", "details": error_detail}), 500
    except FileNotFoundError: return jsonify({"status": "error", "message": "Caddy command not found."}), 500
    except subprocess.TimeoutExpired: return jsonify({"status": "error", "message": "Reload command timed out."}), 500
    except Exception as e: return jsonify({"status": "error", "message": f"Error: {e}"}), 500

# --- Real Log Data Processing for Stats Page ---
# --- Stats Routes (backed by stats_aggregator) ---

@app.route('/stats')
@login_required
def stats_page():
    return render_template('stats.html', username=session.get('username'))

@app.route('/api/stats')
@login_required
def get_stats():
    from urllib.parse import unquote
    period = request.args.get('period', '7d')
    host = request.args.get('host', None)
    if host:
        host = unquote(host)

    # Process any new log entries incrementally
    try:
        stats_aggregator.process_new_logs(app.config['CADDY_ACCESS_LOG_FILE'])
    except Exception as e:
        print(f"Error processing new logs: {e}")

    # Run rollup (throttled internally to once every 6 hours)
    try:
        stats_aggregator.rollup_old_buckets()
    except Exception as e:
        print(f"Error during stats rollup: {e}")

    # Get aggregated stats for the requested period/host
    try:
        stats_data = stats_aggregator.get_stats(period, host)
    except Exception as e:
        print(f"Error getting stats: {e}")
        stats_data = stats_aggregator._empty_stats(period, host)
        stats_data["log_read_error"] = f"Server error retrieving stats: {e}"

    # If no data at all, try auto-configuring Caddy logging
    if not stats_data["log_read_error"] and not stats_aggregator.has_data():
        logging_configured = False
        if not app.config['CADDY_ACCESS_LOG_FILE'].exists():
            # Try to auto-configure logging in the Caddyfile
            try:
                from flask import session as _session
                result = _configure_caddyfile_logging_internal()
                if result.get("status") in ("success", "warning"):
                    stats_data["log_read_error"] = (
                        "Logging was not configured. Auto-configured JSON logging in Caddyfile. "
                        "Stats will appear after Caddy generates new log entries."
                    )
                    logging_configured = True
            except Exception as e:
                print(f"Auto-configure logging attempt failed: {e}")

        if not logging_configured and not app.config['CADDY_ACCESS_LOG_FILE'].exists():
            stats_data["log_read_error"] = (
                f"Log file {app.config['CADDY_ACCESS_LOG_FILE']} not found. "
                "Configure Caddy for JSON logging to stdout."
            )
        elif not logging_configured and stats_data["total_requests"] == 0:
            stats_data["log_read_error"] = (
                "No processable log entries found. "
                "Check that Caddy is configured for JSON logging."
            )

    return jsonify(stats_data)


@app.route('/api/stats/hosts')
@login_required
def get_stats_hosts():
    """Return the list of hosts that have stats data."""
    try:
        hosts = stats_aggregator.get_available_hosts()
    except Exception as e:
        print(f"Error getting available hosts: {e}")
        hosts = []
    return jsonify(hosts)

# --- Helper functions for Caddyfile parsing ---

def _find_matching_brace(content, start):
    """Find the position of the closing brace matching the opening brace at 'start'.
    Handles nested braces, quoted strings, and comments.
    Returns the index of the matching '}'", or -1 if not found."""
    if start >= len(content) or content[start] != '{':
        return -1
    depth = 0
    i = start
    in_string = False
    in_comment = False
    while i < len(content):
        char = content[i]
        if in_comment:
            if char == '\n':
                in_comment = False
            i += 1
            continue
        if in_string:
            if char == '\\':
                i += 2
                continue
            if char == '"':
                in_string = False
            i += 1
            continue
        if char == '#':
            in_comment = True
            i += 1
            continue
        if char == '"':
            in_string = True
            i += 1
            continue
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _remove_directive_block(content, directive_name):
    """Remove a top-level directive block (e.g., 'log { ... }') from content.
    Handles nested braces within the block. Returns (new_content, found)."""
    pattern = re.compile(r'^(\s*)' + re.escape(directive_name) + r'\s*\{', re.MULTILINE)
    match = pattern.search(content)
    if not match:
        return content, False
    brace_start = match.end() - 1
    brace_end = _find_matching_brace(content, brace_start)
    if brace_end == -1:
        return content, False
    line_start = content.rfind('\n', 0, match.start()) + 1 if match.start() > 0 else 0
    line_end = content.find('\n', brace_end)
    if line_end == -1:
        line_end = len(content)
    else:
        line_end += 1
    return content[:line_start] + content[line_end:], True


# --- API to configure logging in Caddyfile ---

def _configure_caddyfile_logging_internal():
    """Internal: add/modify global log config in Caddyfile for JSON stdout logging.
    Returns a dict with 'status' and 'message' keys (no HTTP status code).
    Does NOT require request context — can be called from the stats API."""
    caddyfile_path = CADDY_CONFIG_FILE
    desired_log_config = "\tlog {\n\t\toutput stdout\n\t\tformat json {\n\t\t\ttime_format rfc3339\n\t\t}\n\t\tlevel INFO\n\t}"

    if not caddyfile_path.exists():
        return {"status": "error", "message": f"Caddyfile not found at {caddyfile_path}."}

    content = caddyfile_path.read_text(encoding='utf-8')

    # Find the global block using proper brace matching
    global_open = None
    for i, ch in enumerate(content):
        if ch in (' ', '\t', '\n', '\r'):
            continue
        elif ch == '{':
            global_open = i
        break

    if global_open is not None:
        global_close = _find_matching_brace(content, global_open)
        if global_close == -1:
            return {"status": "error", "message": "Malformed Caddyfile: unmatched opening brace in global block."}

        inner_content = content[global_open + 1:global_close]
        inner_content, _ = _remove_directive_block(inner_content, 'log')
        new_inner = inner_content.rstrip() + '\n' + desired_log_config + '\n'
        new_content = content[:global_open + 1] + new_inner + content[global_close:]
    else:
        new_content = "{\n" + desired_log_config + "\n}\n\n" + content

    caddyfile_path.write_text(new_content, encoding='utf-8')

    try:
        command = ["caddy", "reload", "--config", str(CADDY_CONFIG_FILE), "--adapter", "caddyfile"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
        if result.returncode == 0:
            return {"status": "success", "message": "Caddyfile updated for JSON logging and Caddy reloaded successfully."}
        else:
            error_detail = (result.stderr or result.stdout or "Unknown error during reload.")[:500]
            return {"status": "warning",
                "message": f"Caddyfile updated for JSON logging, but Caddy reload failed (Code: {result.returncode}).",
                "details": error_detail}
    except Exception as e:
        return {"status": "error", "message": f"Error during Caddy reload: {e}"}


@app.route('/api/caddyfile/configure_logging', methods=['POST'])
@login_required
def configure_caddyfile_logging():
    """Attempts to add or modify the global log configuration in the Caddyfile
    to use JSON to stdout. Delegates to _configure_caddyfile_logging_internal()."""
    result = _configure_caddyfile_logging_internal()
    status_code = 500 if result.get("status") == "error" else 200
    return jsonify(result), status_code


# Entry point to run the application
if __name__ == '__main__':
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    CADDY_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Initialize stats database
    stats_aggregator.init_stats_db(STATS_DB_PATH)

    if not PREFERENCES_FILE.exists():
        print(f"Dev: Creating default preferences file at {PREFERENCES_FILE}")
        save_preferences(DEFAULT_PREFERENCES) 

    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('FLASK_PORT', 5000)))
