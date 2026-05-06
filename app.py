# Usage:
# Description: Flask backend application for the CaddyPanel interface.
# ... (rest of initial comments and imports) ...

import json
import os
import re 
import subprocess
import time
from pathlib import Path
from functools import wraps
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request
import urllib.error
import socket
from flask import (Flask, render_template, url_for, request, jsonify, abort,
                   session, redirect, flash, Response)
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta, timezone
import secrets
import stats_aggregator

# --- Configuration ---
# ... (unchanged)
APP_DATA_DIR = Path(os.environ.get('APP_DATA_DIR', '.')).resolve() 
CADDY_CONFIG_FILE = Path(os.environ.get('CADDY_CONFIG', os.environ.get('CADDY_CONFIG_FILE', '/etc/caddy/Caddyfile'))).resolve()
CADDY_ACCESS_LOG_FILE = Path(os.environ.get('CADDY_ACCESS_LOG_FILE', '/var/log/caddy_panel/caddy_access.json.log'))

STATS_DB_PATH = APP_DATA_DIR / 'stats.db'
PREFERENCES_FILE = APP_DATA_DIR / 'preferences.json'
USERS_FILE = APP_DATA_DIR / 'users.json'
BASE_DIR = Path('.').resolve() 

DEFAULT_PREFERENCES = {
    "theme": "theme-light-gray",
    "caddyfilePath": str(CADDY_CONFIG_FILE), 
    "globalAdminEmail": "", 
    "maxmindAccountId": "", 
    "maxmindLicenseKey": "",
    "defaultAuthentikEnabled": False, 
    "defaultAuthentikOutpostUrl": "http://authentik.local:9000", 
    "defaultAuthentikUri": "/outpost.goauthentik.io/auth/caddy", 
    "defaultAuthentikCopyHeaders": "X-Authentik-Username X-Authentik-Groups X-Authentik-Email X-Authentik-Name X-Authentik-Uid X-Authentik-Jwt X-Authentik-Meta-Jwks", 
    "defaultAuthentikTrustedProxies": "private_ranges", 
    "defaultSkipTlsVerify": False,
    "geoBlockMode": "off",
    "geoBlockCountries": []
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

# --- GeoIP check cache for forward-auth endpoint ---
_geo_check_cache = OrderedDict()
_GEO_CHECK_MAX = 10000
_GEO_CHECK_TTL = 300  # 5 minutes

FLASK_PORT = int(os.environ.get('FLASK_PORT', 5000))

def _geo_check_get_country(ip):
    """Resolve IP to country code with LRU+TTL cache."""
    now = time.time()
    if ip in _geo_check_cache:
        country, ts = _geo_check_cache[ip]
        if now - ts < _GEO_CHECK_TTL:
            _geo_check_cache.move_to_end(ip)
            return country
        del _geo_check_cache[ip]
    country = stats_aggregator._resolve_country(ip)
    _geo_check_cache[ip] = (country, now)
    if len(_geo_check_cache) > _GEO_CHECK_MAX:
        _geo_check_cache.popitem(last=False)
    return country

# Configure GeoIP (optional – if mmdb file exists at the configured path)
GEOIP_DB_PATH = os.environ.get('GEOIP_DB_PATH', str(APP_DATA_DIR / 'GeoLite2-Country.mmdb'))
if Path(GEOIP_DB_PATH).is_file():
    stats_aggregator.configure_geoip(GEOIP_DB_PATH)
else:
    print(f"GeoIP: database not found at {GEOIP_DB_PATH}. Country statistics will be unavailable.")
    print("       To enable: set GEOIP_DB_PATH or place GeoLite2-Country.mmdb in APP_DATA_DIR.")
    print("       Get a free license key at https://www.maxmind.com/en/geolite2/signup")

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
    prefs = load_preferences()
    # Don't expose credentials in full — just indicate if set
    if prefs.get('maxmindAccountId'):
        prefs['maxmindAccountId'] = '****' + prefs['maxmindAccountId'][-3:]
    else:
        prefs['maxmindAccountId'] = ''
    if prefs.get('maxmindLicenseKey'):
        prefs['maxmindLicenseKey'] = '********' + prefs['maxmindLicenseKey'][-4:]
    else:
        prefs['maxmindLicenseKey'] = ''
    return jsonify(prefs)

@app.route('/api/preferences', methods=['POST'])
@login_required
def post_preferences():
    # ... (existing code for post_preferences - unchanged)
    try:
        new_prefs_input = request.get_json()
        if not isinstance(new_prefs_input, dict): return jsonify({"status": "error", "message": "Invalid data format"}), 400
        # --- Detect geo-blocking changes ---
        current_prefs = load_preferences()
        old_geo_mode = current_prefs.get('geoBlockMode', 'off')
        old_geo_countries = current_prefs.get('geoBlockCountries', [])
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
                elif key == "maxmindLicenseKey" and value and value.startswith('********'): value = current_prefs.get(key, '')  # Keep existing key if masked value sent
                elif key == "maxmindAccountId" and value and value.startswith('****'): value = current_prefs.get(key, '')  # Keep existing account ID if masked value sent
                if not any(err.startswith(f"Invalid type for '{key}'") or (f"Invalid format for '{key}'" in err) for err in validation_errors):
                     validated_prefs[key] = value
                else: validated_prefs[key] = current_prefs.get(key, default_value)
            else: validated_prefs[key] = current_prefs.get(key, default_value)
        if validation_errors:
            if save_preferences(validated_prefs): return jsonify({"status": "warning", "message": "Preferences saved with errors.", "errors": validation_errors, "saved_prefs": validated_prefs}), 200
            else: return jsonify({"status": "error", "message": "Failed to save preferences with errors"}), 500
        if save_preferences(validated_prefs):
            # --- Handle geo-blocking Caddyfile changes ---
            new_geo_mode = validated_prefs.get('geoBlockMode', 'off')
            new_geo_countries = validated_prefs.get('geoBlockCountries', [])
            geo_mode_changed = (old_geo_mode != new_geo_mode)
            geo_countries_changed = (old_geo_countries != new_geo_countries)
            if geo_mode_changed or geo_countries_changed:
                _apply_geoblocking_to_caddyfile(new_geo_mode != 'off' and bool(new_geo_countries))

            # Auto-download GeoIP DB if credentials provided and DB not yet present
            account_id = validated_prefs.get('maxmindAccountId', '')
            license_key = validated_prefs.get('maxmindLicenseKey', '')
            if account_id and license_key and not stats_aggregator.is_geoip_available():
                geoip_ok, geoip_msg = _try_geoip_download_and_configure(account_id, license_key)
                if geoip_ok:
                    return jsonify({"status": "success", "message": f"Preferences saved. {geoip_msg}", "saved_prefs": validated_prefs})
                else:
                    return jsonify({"status": "warning", "message": f"Preferences saved, but GeoIP setup failed: {geoip_msg}", "saved_prefs": validated_prefs})
            return jsonify({"status": "success", "message": "Preferences saved", "saved_prefs": validated_prefs})
        else: return jsonify({"status": "error", "message": "Failed to save preferences"}), 500
    except json.JSONDecodeError: return jsonify({"status": "error", "message": "Invalid JSON"}), 400
    except Exception as e:
        print(f"Error in post_preferences: {e}")
        return jsonify({"status": "error", "message": "Internal server error"}), 500


def _try_geoip_download_and_configure(account_id, license_key):
    """Download GeoLite2-Country.mmdb and configure GeoIP.
    Strategy: 1) geoipupdate (most reliable), 2) HTTP fallback.
    Returns (success: bool, message: str)."""
    geoip_path = Path(os.environ.get('GEOIP_DB_PATH', str(APP_DATA_DIR / 'GeoLite2-Country.mmdb')))

    # If already downloaded, just reconfigure
    if geoip_path.is_file():
        stats_aggregator.configure_geoip(str(geoip_path))
        return True, f"GeoIP database already present at {geoip_path}"

    if not account_id or not license_key:
        return False, "MaxMind Account ID and License Key are both required."

    import shutil as shutil_mod, subprocess, tempfile

    # --- Strategy 1: geoipupdate (most reliable, uses MaxMind's own protocol) ---
    geoipupdate_bin = shutil_mod.which('geoipupdate')
    if geoipupdate_bin:
        print(f"GeoIP: trying geoipupdate at {geoipupdate_bin}")
        conf_content = (
            f"AccountID {account_id}\n"
            f"LicenseKey {license_key}\n"
            f"EditionIDs GeoLite2-Country\n"
            f"DatabaseDirectory {geoip_path.parent}\n"
        )
        conf_path = APP_DATA_DIR / 'GeoIP.conf'
        try:
            conf_path.write_text(conf_content)
            result = subprocess.run(
                [geoipupdate_bin, '-f', str(conf_path), '-d', str(geoip_path.parent)],
                capture_output=True, text=True, timeout=120,
            )
            print(f"GeoIP: geoipupdate stdout: {result.stdout}")
            print(f"GeoIP: geoipupdate stderr: {result.stderr}")
            if result.returncode == 0 and geoip_path.is_file():
                stats_aggregator.configure_geoip(str(geoip_path))
                return True, f"GeoIP database downloaded via geoipupdate ({geoip_path.stat().st_size / 1024 / 1024:.1f} MB)"
            else:
                stderr_short = (result.stderr or result.stdout or '')[:300]
                print(f"GeoIP: geoipupdate failed (code {result.returncode}): {stderr_short}")
        except Exception as e:
            print(f"GeoIP: geoipupdate error: {e}")
    else:
        print("GeoIP: geoipupdate not found, falling back to HTTP download.")

    # --- Strategy 2: MaxMind download API via HTTP ---
    import tarfile as tarfile_mod
    import urllib.request as urllib_req
    import urllib.error as urllib_err
    import base64

    try:
        url = "https://download.maxmind.com/app/geoip_download?edition_id=GeoLite2-Country&suffix=tar.gz"
        credentials = base64.b64encode(f"{account_id}:{license_key}".encode()).decode()
        req = urllib_req.Request(url, headers={"Authorization": f"Basic {credentials}"})
        print(f"GeoIP: trying HTTP download from MaxMind API...")

        tmp_tar = Path(tempfile.mktemp(suffix='.tar.gz'))
        with urllib_req.urlopen(req, timeout=60) as resp:
            with open(str(tmp_tar), 'wb') as f:
                shutil_mod.copyfileobj(resp, f)

        with tarfile_mod.open(str(tmp_tar), 'r:gz') as tar:
            for member in tar.getmembers():
                if member.name.endswith('GeoLite2-Country.mmdb'):
                    member.name = os.path.basename(member.name)
                    tar.extract(member, str(geoip_path.parent))
                    break

        tmp_tar.unlink(missing_ok=True)

        if geoip_path.is_file():
            stats_aggregator.configure_geoip(str(geoip_path))
            return True, f"GeoIP database downloaded successfully ({geoip_path.stat().st_size / 1024 / 1024:.1f} MB)"
        else:
            return False, "Download succeeded but mmdb file not found in archive."
    except urllib_err.HTTPError as e:
        body = ''
        try: body = e.read(200).decode('utf-8', errors='replace')
        except: pass
        if e.code == 401:
            return False, (f"Authentication failed (HTTP 401). You may need to: "
                           f"1) Accept the GeoLite2 EULA at https://www.maxmind.com/en/geolite2/eula "
                           f"2) Regenerate your License Key at https://www.maxmind.com/en/accounts/{account_id}/license-key")
        return False, f"MaxMind API returned HTTP {e.code}: {body or 'Unknown error'}"
    except Exception as e:
        try: tmp_tar.unlink(missing_ok=True)
        except: pass
        return False, f"GeoIP download failed: {e}"


@app.route('/api/geoip/download', methods=['POST'])
@login_required
def api_geoip_download():
    """Trigger GeoIP database download using the stored MaxMind credentials."""
    prefs = load_preferences()
    account_id = prefs.get('maxmindAccountId', '')
    license_key = prefs.get('maxmindLicenseKey', '')
    if not account_id or not license_key:
        return jsonify({"status": "error", "message": "MaxMind Account ID and License Key are both required. Enter them in Preferences first."}), 400

    success, message = _try_geoip_download_and_configure(account_id, license_key)
    if success:
        return jsonify({"status": "success", "message": message, "geoip_available": stats_aggregator.is_geoip_available()})
    else:
        return jsonify({"status": "error", "message": message, "geoip_available": False}), 500


@app.route('/api/geoip/test', methods=['POST'])
@login_required
def api_geoip_test():
    """Test MaxMind credentials without downloading the full database."""
    prefs = load_preferences()
    account_id = prefs.get('maxmindAccountId', '')
    license_key = prefs.get('maxmindLicenseKey', '')
    if not account_id or not license_key:
        return jsonify({"status": "error", "message": "Enter both Account ID and License Key first."}), 400

    try:
        import urllib.request, base64, shutil, subprocess

        # First check if geoipupdate is available
        geoipupdate_bin = shutil.which('geoipupdate')
        if geoipupdate_bin:
            # Test with geoipupdate --help (doesn't download, just checks if binary works)
            return jsonify({"status": "success", "message": f"geoipupdate is available at {geoipupdate_bin}. Click Download to use it."})

        # Fallback: test credentials via HTTP HEAD-like request
        url = "https://download.maxmind.com/app/geoip_download?edition_id=GeoLite2-Country&suffix=tar.gz"
        credentials = base64.b64encode(f"{account_id}:{license_key}".encode()).decode()
        req = urllib.request.Request(url, headers={"Authorization": f"Basic {credentials}"})
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            size_mb = int(resp.headers.get('Content-Length', 0)) / 1024 / 1024
            resp.read(1)
            return jsonify({"status": "success", "message": f"Credentials valid! Database available ({size_mb:.1f} MB). Click Download to proceed."})
        except urllib.error.HTTPError as e:
            body = ''
            try: body = e.read(200).decode('utf-8', errors='replace')
            except: pass
            if e.code == 401:
                return jsonify({"status": "error", "message": f"Authentication failed (HTTP 401: {body or 'Invalid credentials'}). You may need to: 1) Accept the GeoLite2 EULA at https://www.maxmind.com/en/geolite2/eula 2) Generate a new License Key at https://www.maxmind.com/en/accounts/{account_id}/license-key"}), 401
            return jsonify({"status": "error", "message": f"HTTP {e.code}: {body or 'Unknown error'}"}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": f"Connection test failed: {e}"}), 500


@app.route('/api/geoip/status', methods=['GET'])
@login_required
def api_geoip_status():
    """Check if GeoIP is currently available."""
    return jsonify({"geoip_available": stats_aggregator.is_geoip_available()})


@app.route('/api/geoip/upload', methods=['POST'])
@login_required
def api_geoip_upload():
    """Upload a GeoLite2-Country.mmdb file manually."""
    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "No file provided."}), 400
    f = request.files['file']
    if not f.filename.endswith('.mmdb'):
        return jsonify({"status": "error", "message": "File must be a .mmdb file."}), 400

    geoip_path = Path(os.environ.get('GEOIP_DB_PATH', str(APP_DATA_DIR / 'GeoLite2-Country.mmdb')))
    try:
        geoip_path.parent.mkdir(parents=True, exist_ok=True)
        f.save(str(geoip_path))
        # Verify the file is valid by trying to configure it
        stats_aggregator.configure_geoip(str(geoip_path))
        if stats_aggregator.is_geoip_available():
            size_mb = geoip_path.stat().st_size / 1024 / 1024
            return jsonify({"status": "success", "message": f"GeoIP database uploaded and activated ({size_mb:.1f} MB).", "geoip_available": True})
        else:
            # File was saved but couldn't be loaded as GeoIP
            geoip_path.unlink(missing_ok=True)
            return jsonify({"status": "error", "message": "File saved but failed to load as GeoIP database. Make sure it's a valid GeoLite2-Country.mmdb."}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": f"Upload failed: {e}"}), 500


@app.route('/api/geoip/check', methods=['GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
def api_geoip_check():
    """Forward-auth endpoint for Caddy geo-blocking.
    Returns 200 if allowed, 403 if blocked.
    No @login_required — Caddy calls this as a subrequest for every visitor request."""
    # If GeoIP not available, allow everything
    if not stats_aggregator.is_geoip_available():
        return Response(status=200)

    prefs = load_preferences()
    mode = prefs.get('geoBlockMode', 'off')
    blocked = prefs.get('geoBlockCountries', [])

    if mode == 'off' or not blocked:
        return Response(status=200)

    # Get client IP: X-Real-Ip > X-Forwarded-For > remote_addr
    ip = (request.headers.get('X-Real-Ip')
          or request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
          or request.remote_addr)
    if not ip:
        return Response(status=200)

    country = _geo_check_get_country(ip)

    if mode == 'block' and country in blocked:
        return Response('Forbidden', status=403, content_type='text/plain')
    if mode == 'allow' and country not in blocked and country != 'Unknown':
        return Response('Forbidden', status=403, content_type='text/plain')

    return Response(status=200)


@app.route('/api/geoip/countries', methods=['GET'])
@login_required
def api_geoip_countries():
    """Return list of countries seen in stats (for the geo-blocking UI)."""
    stats = stats_aggregator.get_stats(period='30d', host=None)
    countries = stats.get('top_countries', [])
    result = [{'code': c['country'], 'name': c['country']} for c in countries]
    return jsonify(result)

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


@app.route('/api/sites/health-check', methods=['POST'])
@login_required
def sites_health_check():
    """Check health of site backends. Expects JSON: {"targets": {"site_addr": "http://host:port", ...}}
    Returns: {"statuses": {"site_addr": "up"|"down"|"unknown", ...}}
    """
    data = request.get_json(silent=True)
    if not data or 'targets' not in data:
        return jsonify({"status": "error", "message": "Missing targets"}), 400

    targets = data['targets']
    if not isinstance(targets, dict) or len(targets) > 100:
        return jsonify({"status": "error", "message": "Invalid targets (max 200 per batch)"}), 400

    def check_one(site_addr, target_url):
        """Check if a backend is reachable. Returns (site_addr, status)."""
        if not target_url:
            return (site_addr, 'unknown')
        url = target_url.strip()
        if not url.startswith(('http://', 'https://')):
            url = 'http://' + url
        try:
            req = urllib.request.Request(url, method='GET', headers={'User-Agent': 'CaddyPanel-HealthCheck/1.0'})
            # Don't read the body - we only care if the server responds
            ctx = None
            if url.startswith('https://'):
                import ssl
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            resp = urllib.request.urlopen(req, timeout=3, context=ctx)
            resp.read(1)  # Read just 1 byte to confirm connection, then close
            resp.close()
            return (site_addr, 'up')
        except urllib.error.HTTPError:
            # Server responded (even with 4xx/5xx) = it's up
            return (site_addr, 'up')
        except (urllib.error.URLError, socket.timeout, OSError, ConnectionRefusedError) as e:
            print(f"Health check DOWN: {site_addr} ({url}) - {type(e).__name__}: {e}")
            return (site_addr, 'down')
        except Exception as e:
            print(f"Health check ERROR: {site_addr} ({url}) - {type(e).__name__}: {e}")
            return (site_addr, 'down')

    statuses = {}
    try:
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(check_one, addr, url): addr for addr, url in targets.items()}
            try:
                for future in as_completed(futures, timeout=30):
                    try:
                        addr, status = future.result()
                        statuses[addr] = status
                    except Exception as e:
                        addr = futures[future]
                        print(f"Health check future error for {addr}: {e}")
                        statuses[addr] = 'down'
            except TimeoutError:
                # Some futures didn't complete in time
                print(f"Health check: {len(targets) - len(statuses)} targets timed out")
    except Exception as e:
        print(f"Health check thread pool error: {e}")

    # Any targets not yet in statuses timed out
    for addr in targets:
        if addr not in statuses:
            statuses[addr] = 'down'

    print(f"Health check results: {statuses}")
    return jsonify({"statuses": statuses})


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


def _add_log_to_site_blocks(content):
    """Add a 'log' directive to every site block that doesn't already have one.
    A site block is a top-level block that is NOT the global block (the one
    starting at the very beginning of the file after optional whitespace)."""
    if not content.strip():
        return content

    # Find global block boundaries to skip it
    global_start = None
    global_end = None
    for i, ch in enumerate(content):
        if ch in (' ', '\t', '\n', '\r'):
            continue
        if ch == '{':
            global_start = i
            global_end = _find_matching_brace(content, i)
        break

    # Find all top-level blocks (opening braces NOT inside the global block)
    blocks = []
    pos = 0
    while pos < len(content):
        idx = content.find('{', pos)
        if idx == -1:
            break
        # Skip if inside global block
        if global_start is not None and global_end is not None:
            if global_start <= idx <= global_end:
                pos = idx + 1
                continue
        # Verify this is a site block opener (has an address before the {)
        line_start = content.rfind('\n', 0, idx) + 1
        line_before = content[line_start:idx].strip()
        if not line_before or line_before.startswith('#'):
            pos = idx + 1
            continue
        close = _find_matching_brace(content, idx)
        if close == -1:
            pos = idx + 1
            continue
        blocks.append((idx, close))
        pos = close + 1

    # Process blocks in reverse order so offsets stay valid
    for block_open, block_close in reversed(blocks):
        block_body = content[block_open + 1:block_close]
        if re.search(r'^\s*log\b', block_body, re.MULTILINE):
            continue  # already has log
        # Determine indentation from existing directives
        indent_match = re.search(r'\n(\s+)\S', block_body)
        indent = indent_match.group(1) if indent_match else '\t'
        # Insert 'log' right after the opening brace
        insert = '\n' + indent + 'log'
        content = content[:block_open + 1] + insert + content[block_open + 1:]

    return content


# --- API to configure logging in Caddyfile ---

def _apply_geoblocking_to_caddyfile(enabled):
    """Apply or remove forward_auth geo-blocking directives in the Caddyfile.
    Called when geo-blocking preferences change. Reloads Caddy if modified."""
    caddyfile_path = CADDY_CONFIG_FILE
    if not caddyfile_path.exists():
        return

    content = caddyfile_path.read_text(encoding='utf-8')
    new_content = _configure_caddyfile_geoblocking(content, enabled, FLASK_PORT)

    if new_content != content:
        caddyfile_path.write_text(new_content, encoding='utf-8')
        try:
            result = subprocess.run(
                ["caddy", "reload", "--config", str(CADDY_CONFIG_FILE), "--adapter", "caddyfile"],
                capture_output=True, text=True, timeout=30, check=False
            )
            if result.returncode != 0:
                print(f"Geo-blocking: Caddy reload failed: {result.stderr or result.stdout}")
        except Exception as e:
            print(f"Geo-blocking: Caddy reload error: {e}")


def _configure_caddyfile_geoblocking(content, enabled, flask_port):
    """Add or remove forward_auth geo-blocking directives in every site block.
    Marked with # CADDYPANEL_GEOBLOCK / # END_CADDYPANEL_GEOBLOCK comments
    so they can be cleanly removed later."""
    GEO_START = '# CADDYPANEL_GEOBLOCK'
    GEO_END = '# END_CADDYPANEL_GEOBLOCK'

    if not content.strip():
        return content

    # --- Step 1: Remove existing geo-block directives ---
    lines = content.split('\n')
    new_lines = []
    skip = False
    for line in lines:
        stripped = line.strip()
        if stripped == GEO_START:
            skip = True
            continue
        if stripped == GEO_END:
            skip = False
            continue
        if not skip:
            new_lines.append(line)
    content = '\n'.join(new_lines)

    if not enabled:
        return content  # Removal only

    # --- Step 2: Find global block boundaries to skip it ---
    global_start = None
    global_end = None
    for i, ch in enumerate(content):
        if ch in (' ', '\t', '\n', '\r'):
            continue
        if ch == '{':
            global_start = i
            global_end = _find_matching_brace(content, i)
        break

    # --- Step 3: Find all top-level site blocks ---
    blocks = []
    pos = 0
    while pos < len(content):
        idx = content.find('{', pos)
        if idx == -1:
            break
        if global_start is not None and global_end is not None:
            if global_start <= idx <= global_end:
                pos = idx + 1
                continue
        line_start = content.rfind('\n', 0, idx) + 1
        line_before = content[line_start:idx].strip()
        if not line_before or line_before.startswith('#'):
            pos = idx + 1
            continue
        close = _find_matching_brace(content, idx)
        if close == -1:
            pos = idx + 1
            continue
        blocks.append((idx, close))
        pos = close + 1

    # --- Step 4: Inject forward_auth into each site block (reverse order) ---
    for block_open, _ in reversed(blocks):
        after_open = content[block_open + 1:block_open + 200]
        indent_match = re.search(r'\n(\t+| +)\S', after_open)
        indent = indent_match.group(1) if indent_match else '\t'
        block_snippet = (
            f'\n{GEO_START}\n'
            f'{indent}@notWebSocket not header Connection *Upgrade*\n'
            f'{indent}forward_auth @notWebSocket localhost:{flask_port} {{\n'
            f'{indent}\turi /api/geoip/check\n'
            f'{indent}}}\n'
            f'{GEO_END}'
        )
        content = content[:block_open + 1] + block_snippet + content[block_open + 1:]

    return content


def _configure_caddyfile_logging_internal():
    """Internal: add/modify global log config in Caddyfile for JSON stdout logging,
    and ensure every site block has a 'log' directive so access logs are emitted.
    Returns a dict with 'status' and 'message' keys (no HTTP status code).
    Does NOT require request context - can be called from the stats API."""
    caddyfile_path = CADDY_CONFIG_FILE
    desired_log_config = "\tlog {\n\t\toutput stdout\n\t\tformat json\n\t\tlevel INFO\n\t}"

    if not caddyfile_path.exists():
        return {"status": "error", "message": f"Caddyfile not found at {caddyfile_path}."}

    content = caddyfile_path.read_text(encoding='utf-8')

    # --- Step 1: Ensure global block has proper log config ---
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
        content = content[:global_open + 1] + new_inner + content[global_close:]
    else:
        content = "{\n" + desired_log_config + "\n}\n\n" + content

    # --- Step 2: Add 'log' directive to every site block that lacks one ---
    content = _add_log_to_site_blocks(content)

    caddyfile_path.write_text(content, encoding='utf-8')

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
