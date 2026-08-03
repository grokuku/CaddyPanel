# Usage:
# Description: Flask backend application for the CaddyPanel interface.
# ... (rest of initial comments and imports) ...

import json
import os
import re
import subprocess
import logging
import tempfile
from pathlib import Path
from functools import wraps
from flask import (Flask, render_template, url_for, request, jsonify, abort,
                   session, redirect, flash)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.exceptions import HTTPException, BadRequest
from werkzeug.middleware.proxy_fix import ProxyFix
from datetime import datetime, timedelta, timezone
import secrets
import time
import stats_aggregator
from caddyfile_parser import (
    find_matching_brace,
    remove_directive_block,
    add_log_to_site_blocks,
    configure_caddyfile_logging as _configure_caddyfile_logging_impl,
)

logger = logging.getLogger(__name__)

# --- Configuration ---
# ... (unchanged)
APP_DATA_DIR = Path(os.environ.get('APP_DATA_DIR', '.')).resolve() 
CADDY_CONFIG_FILE = Path(os.environ.get('CADDY_CONFIG', os.environ.get('CADDY_CONFIG_FILE', '/etc/caddy/Caddyfile'))).resolve()
CADDY_ACCESS_LOG_FILE = Path(os.environ.get('CADDY_ACCESS_LOG_FILE', '/var/log/caddy_panel/caddy_access.json.log'))

STATS_DB_PATH = APP_DATA_DIR / 'stats.db'
PREFERENCES_FILE = APP_DATA_DIR / 'preferences.json'
USERS_FILE = APP_DATA_DIR / 'users.json'
FILE_BROWSE_DIR = Path(os.environ.get('FILE_BROWSE_DIR', str(CADDY_CONFIG_FILE.parent))).resolve()
SENSITIVE_FILES = {'users.json', 'preferences.json', 'stats.db', 'GeoIP.conf', '.env', 'app.py', 'stats_aggregator.py'}

def _is_within_directory(path, base):
    """Check if `path` is within `base` directory. Safe against prefix attacks."""
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False

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
    "defaultSkipTlsVerify": False 
}

app = Flask(__name__)

# Fix request.remote_addr behind a reverse proxy (Caddy)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1) 

_flask_secret = os.environ.get('FLASK_SECRET_KEY')
if not _flask_secret or _flask_secret == 'dev-only-unsafe-default-key-3f9a1z-CHANGE-IN-PROD':
    app.config['SECRET_KEY'] = secrets.token_hex(32)
    logger.warning("FLASK_SECRET_KEY not set or using the default insecure key. A random temporary key was generated. "
          "Sessions will not persist across restarts. Set FLASK_SECRET_KEY environment variable for production use.")
else:
    app.config['SECRET_KEY'] = _flask_secret
app.config['USERS_FILE'] = USERS_FILE
app.config['PREFERENCES_FILE'] = PREFERENCES_FILE
app.config['CADDY_ACCESS_LOG_FILE'] = CADDY_ACCESS_LOG_FILE
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
# Secure cookie by default (requires HTTPS). Set FLASK_COOKIE_SECURE=0 to allow
# the session cookie over plain HTTP (e.g. direct access on port 5000 without a
# TLS-terminating reverse proxy in front).
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('FLASK_COOKIE_SECURE', '1') == '1'
app.config['SESSION_PERMANENT'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=12)

# Initialize stats database (works both with dev server and gunicorn)
stats_aggregator.init_stats_db(STATS_DB_PATH)

# Configure GeoIP (optional – if mmdb file exists at the configured path)
GEOIP_DB_PATH = os.environ.get('GEOIP_DB_PATH', str(APP_DATA_DIR / 'GeoLite2-Country.mmdb'))
if Path(GEOIP_DB_PATH).is_file():
    stats_aggregator.configure_geoip(GEOIP_DB_PATH)
else:
    logger.info(f"GeoIP: database not found at {GEOIP_DB_PATH}. Country statistics will be unavailable.")
    logger.info("       To enable: set GEOIP_DB_PATH or place GeoLite2-Country.mmdb in APP_DATA_DIR.")
    logger.info("       Get a free license key at https://www.maxmind.com/en/geolite2/signup")

# --- User Management Helpers ---
# ... (load_users, save_users, get_admin_user - unchanged)
def load_users():
    users_file_path = app.config['USERS_FILE']
    if not users_file_path.exists(): return {}
    try:
        with open(users_file_path, 'r') as f: return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Error loading users file '{users_file_path}': {e}. Assuming no users.")
        return {}

def save_users(users):
    users_file_path = app.config['USERS_FILE']
    try:
        users_file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(users_file_path, 'w') as f: json.dump(users, f, indent=4)
        return True
    except IOError as e:
        logger.error(f"Error saving users file '{users_file_path}': {e}")
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
        logger.error(f"Error loading preferences file '{prefs_file_path}': {e}. Using defaults.")
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
        logger.error(f"Error saving preferences file '{prefs_file_path}': {e}")
        return False


def _maxmind_credentials_from_env():
    """Return True when both MaxMind credentials are provided via environment variables."""
    return bool(os.environ.get('MAXMIND_ACCOUNT_ID', '').strip()
                and os.environ.get('MAXMIND_LICENSE_KEY', '').strip())


def _get_maxmind_credentials():
    """Resolve MaxMind credentials with environment-variable priority.

    Priority: MAXMIND_ACCOUNT_ID / MAXMIND_LICENSE_KEY env vars > preferences.json.
    Returns a tuple (account_id, license_key, source) where source is 'env' or
    'preferences'. If only one env var is set, both are read from preferences.json
    instead (with a warning), to avoid mixing credentials from two sources.
    """
    env_account_id = os.environ.get('MAXMIND_ACCOUNT_ID', '').strip()
    env_license_key = os.environ.get('MAXMIND_LICENSE_KEY', '').strip()
    if env_account_id and env_license_key:
        return env_account_id, env_license_key, 'env'
    if env_account_id or env_license_key:
        logger.warning("Only one of MAXMIND_ACCOUNT_ID / MAXMIND_LICENSE_KEY is set. "
                       "Both are required to use environment variables; "
                       "falling back to credentials stored in preferences.json.")
    prefs = load_preferences()
    return (prefs.get('maxmindAccountId', '') or '',
            prefs.get('maxmindLicenseKey', '') or '',
            'preferences')

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

# --- CSRF Protection (home-grown, no external dependency) ---
def generate_csrf_token():
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(32)
    return session['_csrf_token']

def csrf_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.method in ('POST', 'PUT', 'DELETE'):
            token = request.headers.get('X-CSRFToken') or request.form.get('csrf_token')
            if not token or token != session.get('_csrf_token'):
                abort(400, "CSRF token missing or invalid")
        return f(*args, **kwargs)
    return decorated_function

app.jinja_env.globals['csrf_token'] = generate_csrf_token

# --- Login Rate Limiting (shared across workers via SQLite, in-memory fallback) ---
_login_attempts = {}  # {ip: [timestamps]} - fallback used when SQLite is unavailable
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW = 300  # 5 minutes


def _get_attempt_count(ip, now):
    """Number of recent failed login attempts for an IP within the window.

    Uses the shared SQLite store (stats_aggregator) so every gunicorn worker
    enforces the same limit; falls back to the in-memory dict when SQLite is
    unavailable so rate limiting keeps working."""
    count = stats_aggregator.login_attempts_count(ip, now)
    if count is not None:
        return count
    attempts = [t for t in _login_attempts.get(ip, []) if now - t < _LOGIN_WINDOW]
    _login_attempts[ip] = attempts
    return len(attempts)


def _record_login_failure(ip, now):
    """Record a failed login attempt (SQLite with in-memory fallback)."""
    if not stats_aggregator.login_attempts_add(ip, now):
        attempts = [t for t in _login_attempts.get(ip, []) if now - t < _LOGIN_WINDOW]
        attempts.append(now)
        _login_attempts[ip] = attempts


def _reset_login_attempts(ip):
    """Reset the login-failure counter for an IP after a successful login."""
    if not stats_aggregator.login_attempts_clear(ip):
        _login_attempts.pop(ip, None)

# --- Security Headers ---
@app.after_request
def set_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self' https://cdn.jsdelivr.net"
    )
    return response

# --- Routes ---
# ... (index, setup, login, logout, api/preferences, etc. - unchanged until the stats part)
@app.route('/')
@login_required
def index():
    # ... (existing code for index - unchanged)
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
@csrf_required
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
@csrf_required
def login():
    # ... (existing code for login - unchanged)
    if 'username' in session: return redirect(url_for('index'))
    if not get_admin_user():
         flash("No admin account found. Please set up the administrator.", "info")
         return redirect(url_for('setup'))
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        # --- Rate limiting (shared across workers via SQLite; in-memory fallback) ---
        # ProxyFix (x_for=1) already rewrites request.remote_addr from the
        # trusted X-Forwarded-For header, so it reflects the real client IP.
        # Never trust the raw X-Forwarded-For header here (client-forgeable).
        ip = request.remote_addr or 'unknown'
        now = time.time()
        if _get_attempt_count(ip, now) >= _LOGIN_MAX_ATTEMPTS:
            flash("Too many login attempts. Try again later.", "danger")
            return render_template('login.html')

        users = load_users()
        user_data = users.get(username)
        if user_data and check_password_hash(user_data['password'], password):
            # Regenerate session to prevent session fixation
            session.clear()
            session['username'] = username
            _reset_login_attempts(ip)
            flash(f"Welcome back, {username}!", "success")
            return redirect(url_for('index'))
        else:
            _record_login_failure(ip, now)
            flash("Invalid username or password.", "danger")
    return render_template('login.html')

@app.route('/api/change-password', methods=['POST'])
@login_required
@csrf_required
def api_change_password():
    """Change the password for the currently logged-in user."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"status": "error", "message": "Invalid JSON body."}), 400

    current_password = data.get('current_password', '')
    new_password = data.get('new_password', '')
    confirm_password = data.get('confirm_password', '')

    if not current_password or not new_password or not confirm_password:
        return jsonify({"status": "error", "message": "All fields are required."}), 400

    if new_password != confirm_password:
        return jsonify({"status": "error", "message": "New passwords do not match."}), 400

    if len(new_password) < 8:
        return jsonify({"status": "error", "message": "New password must be at least 8 characters long."}), 400

    users = load_users()
    username = session.get('username')
    user_data = users.get(username)

    if not user_data:
        return jsonify({"status": "error", "message": "User not found."}), 404

    if not check_password_hash(user_data['password'], current_password):
        return jsonify({"status": "error", "message": "Current password is incorrect."}), 403

    # Update the password
    user_data['password'] = generate_password_hash(new_password)
    users[username] = user_data

    if save_users(users):
        # Regenerate session to invalidate any stolen cookies.
        # session.clear() also wipes the CSRF token, so rebuild the session
        # exactly like a fresh login: restore the username AND mint a new
        # CSRF token so the next page load stays authenticated and the CSRF
        # protection keeps working on the new session.
        session.clear()
        session['username'] = username
        generate_csrf_token()
        return jsonify({"status": "success", "message": "Password changed successfully."})
    else:
        return jsonify({"status": "error", "message": "Failed to save password. Check server logs."}), 500


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
    account_id, license_key, creds_source = _get_maxmind_credentials()
    # Don't expose credentials in full — just indicate if set
    if account_id:
        prefs['maxmindAccountId'] = '****' + account_id[-3:]
    else:
        prefs['maxmindAccountId'] = ''
    if license_key:
        prefs['maxmindLicenseKey'] = '********' + license_key[-4:]
    else:
        prefs['maxmindLicenseKey'] = ''
    # Indicate the source of the credentials ('env' or 'preferences'), never the value.
    prefs['maxmindCredentialsSource'] = creds_source if (account_id and license_key) else ''
    return jsonify(prefs)

@app.route('/api/preferences', methods=['POST'])
@login_required
@csrf_required
def post_preferences():
    # ... (existing code for post_preferences - unchanged)
    try:
        new_prefs_input = request.get_json()
        if not isinstance(new_prefs_input, dict): return jsonify({"status": "error", "message": "Invalid data format"}), 400
        current_prefs = load_preferences()
        _, _, creds_source = _get_maxmind_credentials()
        creds_from_env = (creds_source == 'env')
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
                elif key == "maxmindLicenseKey":
                    if creds_from_env:
                        # Environment variables take priority: never persist MaxMind
                        # credentials in preferences.json when MAXMIND_ACCOUNT_ID /
                        # MAXMIND_LICENSE_KEY are set.
                        value = ''
                    elif value and value.startswith('********'): value = current_prefs.get(key, '')  # Keep existing key if masked value sent
                elif key == "maxmindAccountId":
                    if creds_from_env:
                        # Environment variables take priority: never persist MaxMind
                        # credentials in preferences.json when MAXMIND_ACCOUNT_ID /
                        # MAXMIND_LICENSE_KEY are set.
                        value = ''
                    elif value and value.startswith('****'): value = current_prefs.get(key, '')  # Keep existing account ID if masked value sent
                if not any(err.startswith(f"Invalid type for '{key}'") or (f"Invalid format for '{key}'" in err) for err in validation_errors):
                     validated_prefs[key] = value
                else: validated_prefs[key] = current_prefs.get(key, default_value)
            else:
                if creds_from_env and key in ('maxmindAccountId', 'maxmindLicenseKey'):
                    # Env vars take priority: purge any plaintext credentials
                    # previously stored in preferences.json.
                    validated_prefs[key] = ''
                else:
                    validated_prefs[key] = current_prefs.get(key, default_value)
        if validation_errors:
            if save_preferences(validated_prefs): return jsonify({"status": "warning", "message": "Preferences saved with errors.", "errors": validation_errors, "saved_prefs": validated_prefs}), 200
            else: return jsonify({"status": "error", "message": "Failed to save preferences with errors"}), 500
        if save_preferences(validated_prefs):
            # Auto-download GeoIP DB if credentials available (env has priority) and DB not yet present
            account_id, license_key, _ = _get_maxmind_credentials()
            if account_id and license_key and not stats_aggregator.is_geoip_available():
                geoip_ok, geoip_msg = _try_geoip_download_and_configure(account_id, license_key)
                if geoip_ok:
                    return jsonify({"status": "success", "message": f"Preferences saved. {geoip_msg}", "saved_prefs": validated_prefs})
                else:
                    return jsonify({"status": "warning", "message": f"Preferences saved, but GeoIP setup failed: {geoip_msg}", "saved_prefs": validated_prefs})
            return jsonify({"status": "success", "message": "Preferences saved", "saved_prefs": validated_prefs})
        else: return jsonify({"status": "error", "message": "Failed to save preferences"}), 500
    # request.get_json() raises werkzeug.exceptions.BadRequest (not
    # json.JSONDecodeError) for a malformed body; catch it explicitly so the
    # API keeps returning a proper JSON 400 instead of a 500.
    except (json.JSONDecodeError, BadRequest): return jsonify({"status": "error", "message": "Invalid JSON"}), 400
    except Exception as e:
        logger.error(f"Error in post_preferences: {e}")
        return jsonify({"status": "error", "message": "Internal server error"}), 500


def _try_geoip_download_and_configure(account_id=None, license_key=None):
    """Download GeoLite2-Country.mmdb and configure GeoIP.
    Strategy: 1) geoipupdate (most reliable), 2) HTTP fallback.
    Credentials are resolved via _get_maxmind_credentials() (env priority)
    when not passed explicitly.
    Returns (success: bool, message: str)."""
    if not account_id or not license_key:
        account_id, license_key, _ = _get_maxmind_credentials()
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
        logger.info(f"GeoIP: trying geoipupdate at {geoipupdate_bin}")
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
            logger.info(f"GeoIP: geoipupdate stdout: {result.stdout}")
            logger.info(f"GeoIP: geoipupdate stderr: {result.stderr}")
            if result.returncode == 0 and geoip_path.is_file():
                stats_aggregator.configure_geoip(str(geoip_path))
                return True, f"GeoIP database downloaded via geoipupdate ({geoip_path.stat().st_size / 1024 / 1024:.1f} MB)"
            else:
                stderr_short = (result.stderr or result.stdout or '')[:300]
                logger.warning(f"GeoIP: geoipupdate failed (code {result.returncode}): {stderr_short}")
        except Exception as e:
            logger.error(f"GeoIP: geoipupdate error: {e}")
        finally:
            conf_path.unlink(missing_ok=True)
    else:
        logger.info("GeoIP: geoipupdate not found, falling back to HTTP download.")

    # --- Strategy 2: MaxMind download API via HTTP ---
    import tarfile as tarfile_mod
    import urllib.request as urllib_req
    import urllib.error as urllib_err
    import base64

    try:
        url = "https://download.maxmind.com/app/geoip_download?edition_id=GeoLite2-Country&suffix=tar.gz"
        credentials = base64.b64encode(f"{account_id}:{license_key}".encode()).decode()
        req = urllib_req.Request(url, headers={"Authorization": f"Basic {credentials}"})
        logger.info(f"GeoIP: trying HTTP download from MaxMind API...")

        # tempfile.NamedTemporaryFile is race-free (unlike tempfile.mktemp)
        tmp_tar = tempfile.NamedTemporaryFile(delete=False, suffix='.tar.gz')
        tmp_tar_path = tmp_tar.name
        tmp_tar.close()
        try:
            with urllib_req.urlopen(req, timeout=60) as resp:
                with open(tmp_tar_path, 'wb') as f:
                    shutil_mod.copyfileobj(resp, f)

            with tarfile_mod.open(tmp_tar_path, 'r:gz') as tar:
                for member in tar.getmembers():
                    if member.name.endswith('GeoLite2-Country.mmdb'):
                        member.name = os.path.basename(member.name)
                        tar.extract(member, str(geoip_path.parent))
                        break

            if geoip_path.is_file():
                stats_aggregator.configure_geoip(str(geoip_path))
                return True, f"GeoIP database downloaded successfully ({geoip_path.stat().st_size / 1024 / 1024:.1f} MB)"
            else:
                return False, "Download succeeded but mmdb file not found in archive."
        finally:
            try:
                Path(tmp_tar_path).unlink(missing_ok=True)
            except OSError:
                pass
    except urllib_err.HTTPError as e:
        body = ''
        try: body = e.read(200).decode('utf-8', errors='replace')
        except: pass
        if e.code == 401:
            return False, (f"Authentication failed (HTTP 401). You may need to: "
                           f"1) Accept the GeoLite2 EULA at https://www.maxmind.com/en/geolite2/eula "
                           f"2) Regenerate your License Key at https://www.maxmind.com/en/account")
        return False, f"MaxMind API returned HTTP {e.code}: {body or 'Unknown error'}"
    except Exception as e:
        return False, f"GeoIP download failed: {e}"


@app.route('/api/geoip/download', methods=['POST'])
@login_required
@csrf_required
def api_geoip_download():
    """Trigger GeoIP database download using the MaxMind credentials (env has priority)."""
    account_id, license_key, _ = _get_maxmind_credentials()
    if not account_id or not license_key:
        return jsonify({"status": "error", "message": "MaxMind Account ID and License Key are both required. Enter them in Preferences first or set MAXMIND_ACCOUNT_ID / MAXMIND_LICENSE_KEY."}), 400

    success, message = _try_geoip_download_and_configure(account_id, license_key)
    if success:
        return jsonify({"status": "success", "message": message, "geoip_available": stats_aggregator.is_geoip_available()})
    else:
        return jsonify({"status": "error", "message": message, "geoip_available": False}), 500


@app.route('/api/geoip/test', methods=['POST'])
@login_required
@csrf_required
def api_geoip_test():
    """Test MaxMind credentials without downloading the full database."""
    account_id, license_key, _ = _get_maxmind_credentials()
    if not account_id or not license_key:
        return jsonify({"status": "error", "message": "Enter both Account ID and License Key first (or set MAXMIND_ACCOUNT_ID / MAXMIND_LICENSE_KEY)."}), 400

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
                return jsonify({"status": "error", "message": f"Authentication failed (HTTP 401: {body or 'Invalid credentials'}). You may need to: 1) Accept the GeoLite2 EULA at https://www.maxmind.com/en/geolite2/eula 2) Generate a new License Key at https://www.maxmind.com/en/account"}), 401
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
@csrf_required
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

@app.route('/api/browse', methods=['GET'])
@login_required
def browse_files():
    # ... (existing code for browse_files - unchanged)
    req_path_str = request.args.get('path', '.')
    browse_base = FILE_BROWSE_DIR
    try:
        requested_path = browse_base.joinpath(req_path_str).resolve()
        if not _is_within_directory(requested_path, browse_base): abort(403)
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
    except HTTPException:
        # Re-raise intentional abort(403)/abort(404) instead of letting the
        # generic handler turn them into a 500 (HTTPException subclasses Exception).
        raise
    except Exception as e:
        logger.error(f"Error in browse_files: {e}")
        abort(500)

@app.route('/api/readfile', methods=['GET'])
@login_required
def read_file():
    # ... (existing code for read_file - unchanged)
    req_path_str = request.args.get('path')
    if not req_path_str: return jsonify({"status": "error", "message": "No path"}), 400
    # Block access to sensitive files
    if Path(req_path_str).name in SENSITIVE_FILES: abort(403)
    try:
        requested_file_path = FILE_BROWSE_DIR.joinpath(req_path_str).resolve()
        if not _is_within_directory(requested_file_path, FILE_BROWSE_DIR): abort(403)
        if not requested_file_path.is_file(): return jsonify({"status": "error", "message": "Not a file"}), 404
        content = requested_file_path.read_text(encoding='utf-8')
        return jsonify({"status": "success", "path": req_path_str, "content": content})
    except HTTPException:
        # Re-raise intentional abort(403) instead of letting the generic
        # handler turn it into a 500 (HTTPException subclasses Exception).
        raise
    except FileNotFoundError: return jsonify({"status": "error", "message": "Not found"}), 404
    except PermissionError: return jsonify({"status": "error", "message": "Permission denied"}), 403
    except Exception as e: return jsonify({"status": "error", "message": f"Error: {e}"}), 500

@app.route('/api/caddyfile/save', methods=['POST'])
@login_required
@csrf_required
def save_caddyfile_content():
    # ... (existing code for save_caddyfile_content - unchanged, flash message removed)
    try:
        data = request.get_json()
        content = data.get('content')
        if content is None: return jsonify({"status": "error", "message": "No content"}), 400
        if not isinstance(content, str) or not content.strip():
            return jsonify({"status": "error", "message": "Caddyfile content must be a non-empty string"}), 400
        if len(content) > 1024 * 1024:
            return jsonify({"status": "error", "message": "Caddyfile too large (max 1MB)"}), 400
        if '`' in content or '$(' in content:
            return jsonify({"status": "error", "message": "Caddyfile contains invalid shell substitution characters"}), 400
        # Écrire dans un fichier temporaire d'abord
        # tempfile.NamedTemporaryFile is race-free (unlike tempfile.mktemp)
        tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.caddyfile', mode='w', encoding='utf-8')
        tmp_path = tmp_file.name
        try:
            tmp_file.write(content)
        finally:
            tmp_file.close()
        try:
            try:
                validate_cmd = ["caddy", "validate", "--config", tmp_path, "--adapter", "caddyfile"]
                result = subprocess.run(validate_cmd, capture_output=True, text=True, timeout=15, check=False)
                if result.returncode != 0:
                    error_detail = (result.stderr or result.stdout or "Unknown validation error.")[:500]
                    return jsonify({"status": "error", "message": "Caddyfile validation failed.", "details": error_detail}), 400
            except FileNotFoundError:
                # caddy binary not available (e.g. dev mode outside the container):
                # skip validation but still save the file.
                logger.warning("caddy binary not found; skipping Caddyfile validation.")
            except OSError as e:
                logger.warning(f"Could not run caddy validation ({e}); skipping validation.")
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        # Si validation OK, écrire le fichier de production
        CADDY_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CADDY_CONFIG_FILE.write_text(content, encoding='utf-8')
        return jsonify({"status": "success", "message": f"Caddyfile saved to {CADDY_CONFIG_FILE}"})
    except PermissionError: return jsonify({"status": "error", "message": f"Permission denied writing to {CADDY_CONFIG_FILE}"}), 500
    except Exception as e: return jsonify({"status": "error", "message": f"Error: {e}"}), 500

@app.route('/api/caddy/reload', methods=['POST'])
@login_required
@csrf_required
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
        logger.error(f"Error processing new logs: {e}")

    # Run rollup (throttled internally to once every 6 hours)
    try:
        stats_aggregator.rollup_old_buckets()
    except Exception as e:
        logger.error(f"Error during stats rollup: {e}")

    # Get aggregated stats for the requested period/host
    try:
        stats_data = stats_aggregator.get_stats(period, host)
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        stats_data = stats_aggregator._empty_stats(period, host)
        stats_data["log_read_error"] = f"Server error retrieving stats: {e}"

    # If no data at all, try auto-configuring Caddy logging
    if not stats_data["log_read_error"] and not stats_aggregator.has_data():
        logging_configured = False
        if not app.config['CADDY_ACCESS_LOG_FILE'].exists():
            # Try to auto-configure logging in the Caddyfile
            try:
                result = _configure_caddyfile_logging_internal()
                if result.get("status") in ("success", "warning"):
                    stats_data["log_read_error"] = (
                        "Logging was not configured. Auto-configured JSON logging in Caddyfile. "
                        "Stats will appear after Caddy generates new log entries."
                    )
                    logging_configured = True
            except Exception as e:
                logger.error(f"Auto-configure logging attempt failed: {e}")

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
        logger.error(f"Error getting available hosts: {e}")
        hosts = []
    return jsonify(hosts)

# --- Caddyfile parsing helpers are imported from caddyfile_parser.py ---
# (find_matching_brace, remove_directive_block, add_log_to_site_blocks,
#  configure_caddyfile_logging imported as _configure_caddyfile_logging_impl)

# --- API to configure logging in Caddyfile ---

def _configure_caddyfile_logging_internal():
    """Internal: add/modify global log config in Caddyfile for JSON stdout logging,
    and ensure every site block has a 'log' directive so access logs are emitted.
    Returns a dict with 'status' and 'message' keys (no HTTP status code).
    Does NOT require request context - can be called from the stats API."""
    result = _configure_caddyfile_logging_impl(str(CADDY_CONFIG_FILE))
    if result.get("status") == "error":
        return result

    try:
        command = ["caddy", "reload", "--config", str(CADDY_CONFIG_FILE), "--adapter", "caddyfile"]
        result_proc = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
        if result_proc.returncode == 0:
            return {"status": "success", "message": "Caddyfile updated for JSON logging and Caddy reloaded successfully."}
        else:
            error_detail = (result_proc.stderr or result_proc.stdout or "Unknown error during reload.")[:500]
            return {"status": "warning",
                "message": f"Caddyfile updated for JSON logging, but Caddy reload failed (Code: {result_proc.returncode}).",
                "details": error_detail}
    except Exception as e:
        return {"status": "error", "message": f"Error during Caddy reload: {e}"}


@app.route('/api/caddyfile/configure_logging', methods=['POST'])
@login_required
@csrf_required
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
        logger.info(f"Dev: Creating default preferences file at {PREFERENCES_FILE}")
        save_preferences(DEFAULT_PREFERENCES) 

    app.run(debug=os.environ.get('FLASK_DEBUG', '0') == '1', host='0.0.0.0', port=int(os.environ.get('FLASK_PORT', 5000)))
