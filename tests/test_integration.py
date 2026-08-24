"""
Integration tests for the CaddyPanel Flask app.

Covers:
  - App startup / import
  - Login flow with the new SQLite-backed rate limiter (5 fails -> blocked,
    reset after success, per-IP isolation, in-memory fallback, table creation,
    expired-entry cleanup)
  - Critical routes (/setup, /login, /logout, /, /api/preferences,
    /api/change-password) and CSRF enforcement on POST routes
  - Caddyfile save/reload endpoints (caddy binary may be absent: graceful
    failure + happy-path via stubbed subprocess)
  - Regression smoke tests on the remaining GET routes
  - Security fixes (audit C1-C3): FLASK_SECRET_KEY placeholder/short-value
    refusal, XFF-forging cannot bypass login rate limiting in direct mode,
    corrupted users.json refuses /setup (fail-safe), atomic writes
  - Follow-up hardening: positive proxy-mode check (TRUSTED_PROXY_COUNT=1
    really wraps wsgi_app with ProxyFix and rewrites remote_addr, via a
    subprocess re-import), valid env FLASK_SECRET_KEY honored as SECRET_KEY,
    /setup save failure feedback, preferences.json persisted via atomic_write,
    stale atomic-write temp files refused by /api/readfile and cleaned up

These tests import the real app with a throwaway APP_DATA_DIR / CADDY_CONFIG
so they never touch the repository's real stats.db or Caddyfile.
"""

import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Environment setup must happen BEFORE `import app` (module-level import)
# ---------------------------------------------------------------------------
_TMP_ROOT = tempfile.mkdtemp(prefix='caddypanel_test_')
os.environ['APP_DATA_DIR'] = _TMP_ROOT
os.environ['CADDY_CONFIG'] = str(Path(_TMP_ROOT) / 'Caddyfile')
os.environ['CADDY_ACCESS_LOG_FILE'] = str(Path(_TMP_ROOT) / 'caddy_access.json.log')
os.environ['GEOIP_DB_PATH'] = str(Path(_TMP_ROOT) / 'GeoLite2-Country.mmdb')
os.environ['FLASK_SECRET_KEY'] = 'integration-test-secret-key-123456'
# Direct mode (default): never trust X-Forwarded-For in these tests, even if a
# developer machine exports TRUSTED_PROXY_COUNT globally.
os.environ.pop('TRUSTED_PROXY_COUNT', None)
# Keep SESSION_COOKIE_SECURE=True (the new default) — Werkzeug's test client
# handles Secure cookies transparently on http, so the production-like config
# is exercised.
os.environ.pop('FLASK_COOKIE_SECURE', None)

import app as app_module  # noqa: E402
import stats_aggregator  # noqa: E402

app = app_module.app

ADMIN_USER = 'admin'
ADMIN_PASS = 'password123'


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset login-attempt state between tests (both DB and in-memory)."""
    yield
    # In-memory fallback state in app.py
    app_module._login_attempts.clear()
    # DB-backed state
    try:
        stats_aggregator.login_attempts_clear('127.0.0.1')
        stats_aggregator.login_attempts_clear('10.0.0.1')
        stats_aggregator.login_attempts_clear('10.0.0.2')
    except Exception:
        pass
    # users.json is recreated by the tests that need it; removing it here
    # isolates tests that leave it corrupted (fail-safe tests).
    Path(app.config['USERS_FILE']).unlink(missing_ok=True)


@pytest.fixture()
def client():
    return app.test_client()


def _create_admin():
    from werkzeug.security import generate_password_hash
    users_file = Path(app.config['USERS_FILE'])
    users_file.parent.mkdir(parents=True, exist_ok=True)
    users_file.write_text(json.dumps(
        {ADMIN_USER: {'password': generate_password_hash(ADMIN_PASS)}}
    ), encoding='utf-8')


def _get_csrf(client):
    with client.session_transaction() as sess:
        return sess.get('_csrf_token')


def _login(client, username=ADMIN_USER, password=ADMIN_PASS, ip='127.0.0.1', csrf=None):
    """POST /login with a valid CSRF token (fetched/created automatically)."""
    if csrf is None:
        with client.session_transaction() as sess:
            sess['_csrf_token'] = 'test-csrf-token'
            csrf = 'test-csrf-token'
    return client.post('/login', data={
        'username': username,
        'password': password,
        'csrf_token': csrf,
    }, environ_base={'REMOTE_ADDR': ip})


# ---------------------------------------------------------------------------
# 1. Startup / import
# ---------------------------------------------------------------------------

def test_app_imports_and_configures_secure_cookie():
    """The app is importable and the new secure-cookie default is on."""
    assert app is not None
    assert app.config['SESSION_COOKIE_SECURE'] is True
    assert app.config['SESSION_COOKIE_HTTPONLY'] is True
    assert app.config['SESSION_COOKIE_SAMESITE'] == 'Lax'


def test_stats_db_and_login_attempts_table_created():
    """init_stats_db() runs at import and creates the login_attempts table."""
    db_path = Path(os.environ['APP_DATA_DIR']) / 'stats.db'
    assert db_path.exists(), 'stats.db was not created'
    conn = sqlite3.connect(str(db_path))
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert 'login_attempts' in tables
    assert 'hourly_stats' in tables


# ---------------------------------------------------------------------------
# 2. Login flow + SQLite rate limiting
# ---------------------------------------------------------------------------

def test_login_success_then_logout(client):
    _create_admin()
    with client.session_transaction() as sess:
        sess['_csrf_token'] = 'csrf-1'

    r = client.post('/login', data={'username': ADMIN_USER, 'password': ADMIN_PASS,
                                    'csrf_token': 'csrf-1'})
    assert r.status_code == 302
    assert r.headers['Location'] == '/'
    with client.session_transaction() as sess:
        assert sess.get('username') == ADMIN_USER

    r = client.get('/')
    assert r.status_code == 200

    r = client.get('/logout')
    assert r.status_code == 302
    assert r.headers['Location'] == '/login'
    with client.session_transaction() as sess:
        assert 'username' not in sess


def test_login_5_failures_then_6th_blocked(client):
    _create_admin()
    with client.session_transaction() as sess:
        sess['_csrf_token'] = 'csrf-2'

    # 5 wrong-password attempts
    for i in range(5):
        r = client.post('/login', data={'username': ADMIN_USER, 'password': 'wrongpass',
                                        'csrf_token': 'csrf-2'})
        assert r.status_code == 200, f'attempt {i+1} failed: {r.status_code}'
        body = r.get_data(as_text=True)
        assert 'Invalid username or password' in body

    # 6th attempt is blocked by the rate limiter
    r = client.post('/login', data={'username': ADMIN_USER, 'password': ADMIN_PASS,
                                    'csrf_token': 'csrf-2'})
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'Too many login attempts' in body
    with client.session_transaction() as sess:
        assert sess.get('username') is None


def test_rate_limit_reset_after_success(client):
    _create_admin()
    with client.session_transaction() as sess:
        sess['_csrf_token'] = 'csrf-3'

    # Fail 2 times (not enough to lock out)
    for _ in range(2):
        client.post('/login', data={'username': ADMIN_USER, 'password': 'wrong',
                                    'csrf_token': 'csrf-3'})

    # Correct password -> success (counter is reset on success)
    r = client.post('/login', data={'username': ADMIN_USER, 'password': ADMIN_PASS,
                                    'csrf_token': 'csrf-3'})
    assert r.status_code == 302, f'expected success redirect, got {r.status_code}'

    # Reset session state to a logged-out one (login success clears the session)
    with client.session_transaction() as sess:
        sess.pop('username', None)
        sess['_csrf_token'] = 'csrf-3b'

    # The counter is now 0: 5 fresh failures must be allowed, only the 6th blocked.
    # (If the counter had NOT been reset, we would be blocked after 3 more failures.)
    for i in range(5):
        r = client.post('/login', data={'username': ADMIN_USER, 'password': 'wrong',
                                        'csrf_token': 'csrf-3b'})
        body = r.get_data(as_text=True)
        assert 'Too many login attempts' not in body, f'blocked at attempt {i + 1}'
        assert 'Invalid username or password' in body
    r = client.post('/login', data={'username': ADMIN_USER, 'password': ADMIN_PASS,
                                    'csrf_token': 'csrf-3b'})
    assert 'Too many login attempts' in r.get_data(as_text=True)


def test_rate_limit_isolated_by_ip(client):
    _create_admin()
    with client.session_transaction() as sess:
        sess['_csrf_token'] = 'csrf-4'

    # IP A gets 5 failures -> blocked
    for _ in range(5):
        client.post('/login', data={'username': ADMIN_USER, 'password': 'wrong',
                                    'csrf_token': 'csrf-4'},
                    environ_base={'REMOTE_ADDR': '10.0.0.1'})
    r = client.post('/login', data={'username': ADMIN_USER, 'password': ADMIN_PASS,
                                    'csrf_token': 'csrf-4'},
                    environ_base={'REMOTE_ADDR': '10.0.0.1'})
    assert 'Too many login attempts' in r.get_data(as_text=True)

    # IP B is unaffected: can still log in successfully
    r = client.post('/login', data={'username': ADMIN_USER, 'password': ADMIN_PASS,
                                    'csrf_token': 'csrf-4'},
                    environ_base={'REMOTE_ADDR': '10.0.0.2'})
    assert r.status_code == 302


def test_rate_limit_in_memory_fallback(client, monkeypatch):
    """When the SQLite store is unavailable, the in-memory dict still enforces."""
    _create_admin()

    def _count(*a, **k):
        return None  # simulate DB unavailable
    def _add(*a, **k):
        return False
    def _clear(*a, **k):
        return False

    monkeypatch.setattr(stats_aggregator, 'login_attempts_count', _count)
    monkeypatch.setattr(stats_aggregator, 'login_attempts_add', _add)
    monkeypatch.setattr(stats_aggregator, 'login_attempts_clear', _clear)

    with client.session_transaction() as sess:
        sess['_csrf_token'] = 'csrf-5'

    for _ in range(5):
        client.post('/login', data={'username': ADMIN_USER, 'password': 'wrong',
                                    'csrf_token': 'csrf-5'})
    r = client.post('/login', data={'username': ADMIN_USER, 'password': ADMIN_PASS,
                                    'csrf_token': 'csrf-5'})
    assert 'Too many login attempts' in r.get_data(as_text=True)

    # The in-memory counter was used (entries recorded)
    assert app_module._login_attempts.get('127.0.0.1'), 'in-memory fallback not populated'


def test_expired_attempts_pruned_and_cleaned_up():
    """Fully-expired rows are removed; count returns 0."""
    ip = '198.51.100.9'
    old = time.time() - 10000  # well beyond the 300s window

    conn = sqlite3.connect(str(Path(os.environ['APP_DATA_DIR']) / 'stats.db'))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO login_attempts (ip, attempts, updated_at) "
            "VALUES (?, ?, ?)",
            (ip, json.dumps([old, old + 1]), old),
        )
        conn.commit()
    finally:
        conn.close()

    count = stats_aggregator.login_attempts_count(ip, time.time())
    assert count == 0

    # Row should have been deleted during the prune
    conn = sqlite3.connect(str(Path(os.environ['APP_DATA_DIR']) / 'stats.db'))
    try:
        row = conn.execute("SELECT 1 FROM login_attempts WHERE ip = ?", (ip,)).fetchone()
    finally:
        conn.close()
    assert row is None, 'expired row was not pruned'


def test_add_prunes_old_entries_and_stores_fresh():
    ip = '198.51.100.10'
    old = time.time() - 10000
    stats_aggregator.login_attempts_add(ip, old)
    count_after_old = stats_aggregator.login_attempts_count(ip, time.time())
    assert count_after_old == 0
    # Add a fresh one, then count should reflect it
    now = time.time()
    stats_aggregator.login_attempts_add(ip, now)
    count = stats_aggregator.login_attempts_count(ip, time.time() + 1)
    assert count == 1
    stats_aggregator.login_attempts_clear(ip)


# ---------------------------------------------------------------------------
# 3. Critical routes + CSRF
# ---------------------------------------------------------------------------

def test_setup_creates_admin_account():
    app_module._login_attempts.clear()
    # Ensure no admin exists yet (other tests may have created one in the
    # shared temp APP_DATA_DIR)
    users_file = Path(app.config['USERS_FILE'])
    users_file.unlink(missing_ok=True)
    client = app.test_client()
    r = client.get('/setup')
    assert r.status_code == 200

    with client.session_transaction() as sess:
        sess['_csrf_token'] = 'setup-csrf'

    r = client.post('/setup', data={
        'username': 'setupadmin',
        'password': 'a-very-strong-password',
        'confirm_password': 'a-very-strong-password',
        'csrf_token': 'setup-csrf',
    })
    assert r.status_code == 302
    assert r.headers['Location'] == '/login'

    users = json.loads(Path(app.config['USERS_FILE']).read_text())
    assert 'setupadmin' in users
    # Recreate the expected admin for subsequent tests
    _create_admin()


def test_csrf_missing_token_rejected_on_post():
    _create_admin()
    client = app.test_client()
    # Missing CSRF on the login form -> 400 (login is csrf_required)
    r = client.post('/login', data={'username': ADMIN_USER, 'password': ADMIN_PASS})
    assert r.status_code == 400

    # For an authenticated route, log the session in first, then post with a
    # wrong CSRF token -> 400 (and not a redirect to /login).
    client2 = app.test_client()
    with client2.session_transaction() as sess:
        sess['username'] = ADMIN_USER
        sess['_csrf_token'] = 'csrf-x'
    r = client2.post('/api/preferences', json={'theme': 'theme-light-gray'},
                     headers={'X-CSRFToken': 'wrong-token'})
    assert r.status_code == 400, r.get_data(as_text=True)


def test_api_preferences_get_and_post(client):
    _create_admin()
    with client.session_transaction() as sess:
        sess['username'] = ADMIN_USER
        sess['_csrf_token'] = 'pref-csrf'

    r = client.get('/api/preferences')
    assert r.status_code == 200
    data = r.get_json()
    assert 'theme' in data
    assert 'caddyfilePath' in data
    assert data.get('maxmindAccountId', None) in ('', None)

    r = client.post('/api/preferences', json={
        'theme': 'theme-dark',
        'globalAdminEmail': 'admin@example.com',
        'csrf_token': 'pref-csrf',
    }, headers={'X-CSRFToken': 'pref-csrf'})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body['status'] in ('success', 'warning')
    assert body['saved_prefs']['theme'] == 'theme-dark'


def test_api_preferences_malformed_json_returns_400(client):
    """Malformed JSON body on /api/preferences must produce 400, not a 500."""
    _create_admin()
    with client.session_transaction() as sess:
        sess['username'] = ADMIN_USER
        sess['_csrf_token'] = 'pref-bad-json-csrf'

    r = client.post('/api/preferences',
                    data='{not-valid-json',
                    content_type='application/json',
                    headers={'X-CSRFToken': 'pref-bad-json-csrf'})
    assert r.status_code == 400, \
        f'expected 400, got {r.status_code}: {r.get_data(as_text=True)}'
    body = r.get_json()
    assert body['status'] == 'error'
    assert 'Invalid JSON' in body['message']


def test_api_change_password(client):
    _create_admin()
    with client.session_transaction() as sess:
        sess['username'] = ADMIN_USER
        sess['_csrf_token'] = 'pw-csrf'

    # Wrong current password -> 403
    r = client.post('/api/change-password', json={
        'current_password': 'wrong',
        'new_password': 'newpassword123',
        'confirm_password': 'newpassword123',
    }, headers={'X-CSRFToken': 'pw-csrf'})
    assert r.status_code == 403

    # Correct current password -> success
    r = client.post('/api/change-password', json={
        'current_password': ADMIN_PASS,
        'new_password': 'newpassword123',
        'confirm_password': 'newpassword123',
    }, headers={'X-CSRFToken': 'pw-csrf'})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()['status'] == 'success'

    # New session must still be authenticated and CSRF still enforced
    assert client.get('/').status_code == 200


def test_index_requires_login(client):
    r = client.get('/')
    assert r.status_code == 302
    assert r.headers['Location'].endswith('/login')


# ---------------------------------------------------------------------------
# 4. Caddyfile save / reload endpoints
# ---------------------------------------------------------------------------

def test_save_caddyfile_without_caddy_binary(client):
    """caddy binary is absent -> validation is skipped, file still saved."""
    _create_admin()
    with client.session_transaction() as sess:
        sess['username'] = ADMIN_USER
        sess['_csrf_token'] = 'save-csrf'

    content = "example.com {\n    root * /var/www\n}\n"
    r = client.post('/api/caddyfile/save', json={'content': content},
                    headers={'X-CSRFToken': 'save-csrf'})
    assert r.status_code == 200, r.get_data(as_text=True)
    saved = Path(os.environ['CADDY_CONFIG']).read_text(encoding='utf-8')
    assert saved == content


def test_save_caddyfile_rejects_bad_input(client):
    _create_admin()
    with client.session_transaction() as sess:
        sess['username'] = ADMIN_USER
        sess['_csrf_token'] = 'save-csrf2'
    # Empty content
    r = client.post('/api/caddyfile/save', json={'content': '   '},
                    headers={'X-CSRFToken': 'save-csrf2'})
    assert r.status_code == 400
    # Shell substitution characters
    r = client.post('/api/caddyfile/save', json={'content': '$(rm -rf /)'},
                    headers={'X-CSRFToken': 'save-csrf2'})
    assert r.status_code == 400


def test_reload_without_caddy_binary_fails_cleanly(client):
    """caddy binary absent -> /api/caddy/reload returns 500 with a clean error."""
    _create_admin()
    with client.session_transaction() as sess:
        sess['username'] = ADMIN_USER
        sess['_csrf_token'] = 'reload-csrf'

    r = client.post('/api/caddy/reload', headers={'X-CSRFToken': 'reload-csrf'})
    assert r.status_code == 500, r.get_data(as_text=True)
    body = r.get_json()
    assert body['status'] == 'error'
    assert 'Caddy command not found' in body['message']


def test_save_and_reload_happy_path_with_stubbed_subprocess(client, monkeypatch):
    """Stub subprocess.run to simulate a working caddy binary."""
    import subprocess

    _create_admin()
    with client.session_transaction() as sess:
        sess['username'] = ADMIN_USER
        sess['_csrf_token'] = 'happy-csrf'

    calls = []

    class FakeResult:
        returncode = 0
        stdout = 'OK'
        stderr = ''

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeResult()

    monkeypatch.setattr(app_module.subprocess, 'run', fake_run)

    content = "example.com {\n    root * /var/www\n}\n"
    r = client.post('/api/caddyfile/save', json={'content': content},
                    headers={'X-CSRFToken': 'happy-csrf'})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert 'validate' in ' '.join(calls[0])

    r = client.post('/api/caddy/reload', headers={'X-CSRFToken': 'happy-csrf'})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()['status'] == 'success'
    assert calls[-1][0] == 'caddy'


# ---------------------------------------------------------------------------
# 5. Regression smoke tests on remaining GET routes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('path', [
    '/stats',
    '/api/stats',
    '/api/stats/hosts',
    '/api/geoip/status',
])
def test_regression_get_routes_no_500(client, path):
    _create_admin()
    with client.session_transaction() as sess:
        sess['username'] = ADMIN_USER
    r = client.get(path)
    assert r.status_code < 500, f'{path} returned {r.status_code}'
    assert r.status_code == 200, f'{path} returned {r.status_code}'


def test_regression_browse_and_readfile(client):
    _create_admin()
    with client.session_transaction() as sess:
        sess['username'] = ADMIN_USER

    # browse the FILE_BROWSE_DIR (parent of the configured Caddyfile)
    r = client.get('/api/browse?path=.')
    assert r.status_code == 200
    data = r.get_json()
    assert 'items' in data

    # readfile with no path -> 400
    r = client.get('/api/readfile')
    assert r.status_code == 400

    # readfile a sensitive file by basename -> 403
    r = client.get('/api/readfile?path=users.json')
    assert r.status_code == 403


def test_regression_browse_nonexistent_path_should_be_404(client):
    _create_admin()
    with client.session_transaction() as sess:
        sess['username'] = ADMIN_USER
    r = client.get('/api/browse?path=nonexistent-dir-xyz')
    assert r.status_code == 404


def test_regression_browse_traversal_should_be_403(client):
    _create_admin()
    with client.session_transaction() as sess:
        sess['username'] = ADMIN_USER
    r = client.get('/api/browse?path=../../etc')
    assert r.status_code == 403


def test_regression_readfile_traversal_should_be_403(client):
    """Path traversal on /api/readfile must produce 403, not a 500."""
    _create_admin()
    with client.session_transaction() as sess:
        sess['username'] = ADMIN_USER
    r = client.get('/api/readfile?path=../../etc/passwd')
    assert r.status_code == 403, \
        f'expected 403, got {r.status_code}: {r.get_data(as_text=True)}'


def test_regression_readfile_nonexistent_should_be_404(client):
    """Reading a non-existent file must produce 404, not a 500."""
    _create_admin()
    with client.session_transaction() as sess:
        sess['username'] = ADMIN_USER
    r = client.get('/api/readfile?path=nonexistent-file-xyz.txt')
    assert r.status_code == 404, \
        f'expected 404, got {r.status_code}: {r.get_data(as_text=True)}'


def test_regression_readfile_existing_file_returns_content(client):
    """Reading an existing file inside FILE_BROWSE_DIR still works."""
    _create_admin()
    Path(os.environ['CADDY_CONFIG']).write_text(
        "example.com {\n    root * /var/www\n}\n", encoding='utf-8')
    with client.session_transaction() as sess:
        sess['username'] = ADMIN_USER
    r = client.get('/api/readfile?path=Caddyfile')
    assert r.status_code == 200, \
        f'expected 200, got {r.status_code}: {r.get_data(as_text=True)}'
    body = r.get_json()
    assert body['status'] == 'success'
    assert 'example.com' in body['content']


def test_caddyfile_configure_logging_endpoint(client, monkeypatch):
    """POST /api/caddyfile/configure_logging works (parser + stub reload)."""
    _create_admin()
    with client.session_transaction() as sess:
        sess['username'] = ADMIN_USER
        sess['_csrf_token'] = 'log-csrf'

    # Write an initial Caddyfile for the parser to work on
    Path(os.environ['CADDY_CONFIG']).write_text(
        "example.com {\n    root * /var/www\n}\n", encoding='utf-8')

    class FakeResult:
        returncode = 0
        stdout = ''
        stderr = ''

    monkeypatch.setattr(app_module.subprocess, 'run',
                        lambda *a, **k: FakeResult())

    r = client.post('/api/caddyfile/configure_logging',
                    headers={'X-CSRFToken': 'log-csrf'})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body['status'] in ('success', 'warning')


# ---------------------------------------------------------------------------
# 5b. Servers-options hardening endpoints (global block + per-site transport)
# ---------------------------------------------------------------------------

def _write_initial_caddyfile():
    Path(os.environ['CADDY_CONFIG']).write_text(
        "example.com {\n    reverse_proxy localhost:8080\n}\n", encoding='utf-8')


def test_configure_servers_options_requires_auth_and_csrf(client):
    _write_initial_caddyfile()
    # No session -> redirect to login.
    r = client.post('/api/caddyfile/configure_servers_options', json={})
    assert r.status_code == 302
    # Session but missing/invalid CSRF -> 400.
    _create_admin()
    with client.session_transaction() as sess:
        sess['username'] = ADMIN_USER
        sess['_csrf_token'] = 'srvopt-csrf'
    r = client.post('/api/caddyfile/configure_servers_options', json={})
    assert r.status_code == 400
    r = client.post('/api/caddyfile/configure_servers_options', json={},
                    headers={'X-CSRFToken': 'wrong-token'})
    assert r.status_code == 400


def test_configure_servers_options_happy_path(client, monkeypatch):
    _write_initial_caddyfile()
    _create_admin()
    with client.session_transaction() as sess:
        sess['username'] = ADMIN_USER
        sess['_csrf_token'] = 'srvopt-happy'

    class FakeResult:
        returncode = 0
        stdout = ''
        stderr = ''

    monkeypatch.setattr(app_module.subprocess, 'run',
                        lambda *a, **k: FakeResult())

    r = client.post('/api/caddyfile/configure_servers_options',
                    json={'idleTimeout': '2m', 'keepAliveInterval': '7s'},
                    headers={'X-CSRFToken': 'srvopt-happy'})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body['parser_status'] == 'created'

    text = Path(os.environ['CADDY_CONFIG']).read_text(encoding='utf-8')
    assert 'idle 2m' in text and 'keepalive_interval 7s' in text
    assert 'reverse_proxy localhost:8080' in text      # site block untouched

    # Second call with the same values -> no-op, file unchanged.
    r = client.post('/api/caddyfile/configure_servers_options',
                    json={'idleTimeout': '2m', 'keepAliveInterval': '7s'},
                    headers={'X-CSRFToken': 'srvopt-happy'})
    assert r.status_code == 200
    assert r.get_json()['parser_status'] == 'unchanged'


def test_harden_site_requires_auth_and_csrf(client):
    _write_initial_caddyfile()
    r = client.post('/api/caddyfile/harden_site', json={'upstream': 'localhost:8080'})
    assert r.status_code == 302
    _create_admin()
    with client.session_transaction() as sess:
        sess['username'] = ADMIN_USER
        sess['_csrf_token'] = 'harden-csrf'
    r = client.post('/api/caddyfile/harden_site', json={'upstream': 'localhost:8080'})
    assert r.status_code == 400
    r = client.post('/api/caddyfile/harden_site', json={'upstream': 'localhost:8080'},
                    headers={'X-CSRFToken': 'wrong-token'})
    assert r.status_code == 400


def test_harden_site_rejects_invalid_upstream(client):
    _create_admin()
    with client.session_transaction() as sess:
        sess['username'] = ADMIN_USER
        sess['_csrf_token'] = 'harden-bad'
    for bad in (None, '', 'a b', 'x{y}'):
        r = client.post('/api/caddyfile/harden_site', json={'upstream': bad},
                        headers={'X-CSRFToken': 'harden-bad'})
        assert r.status_code == 400, bad


def test_harden_site_happy_path(client, monkeypatch):
    _write_initial_caddyfile()
    _create_admin()
    with client.session_transaction() as sess:
        sess['username'] = ADMIN_USER
        sess['_csrf_token'] = 'harden-happy'

    class FakeResult:
        returncode = 0
        stdout = ''
        stderr = ''

    monkeypatch.setattr(app_module.subprocess, 'run',
                        lambda *a, **k: FakeResult())

    r = client.post('/api/caddyfile/harden_site',
                    json={'upstream': 'localhost:8080'},
                    headers={'X-CSRFToken': 'harden-happy'})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()['parser_status'] == 'updated'

    text = Path(os.environ['CADDY_CONFIG']).read_text(encoding='utf-8')
    assert 'flush_interval -1' in text
    assert 'transport http {' in text
    assert 'keepalive_idle 5m' in text and 'keepalive_interval 30s' in text

    # Already hardened -> no-op reported, file untouched.
    before = text
    r = client.post('/api/caddyfile/harden_site',
                    json={'upstream': 'localhost:8080'},
                    headers={'X-CSRFToken': 'harden-happy'})
    assert r.status_code == 200
    assert r.get_json()['parser_status'] == 'unchanged'
    assert Path(os.environ['CADDY_CONFIG']).read_text(encoding='utf-8') == before


def test_preferences_accept_new_server_option_keys(client):
    """POST /api/preferences accepts the new hardening keys and persists them;
    old preferences files on disk keep loading without the new keys."""
    _create_admin()
    with client.session_transaction() as sess:
        sess['username'] = ADMIN_USER
        sess['_csrf_token'] = 'pref-new-csrf'

    new_keys = {
        'globalServersOptionsEnabled': True,
        'globalServersIdleTimeout': '10m',
        'globalKeepAliveInterval': '30s',
        'siteFlushIntervalEnabled': False,
        'siteTransportKeepAliveIdle': '5m',
        'siteTransportKeepAliveInterval': '30s',
    }
    r = client.post('/api/preferences', json=new_keys,
                    headers={'X-CSRFToken': 'pref-new-csrf'})
    assert r.status_code == 200, r.get_data(as_text=True)
    saved = r.get_json()['saved_prefs']
    for key, value in new_keys.items():
        assert saved[key] == value, key

    # Persisted on disk and served back by GET.
    on_disk = json.loads(Path(app_module.PREFERENCES_FILE).read_text(encoding='utf-8'))
    for key, value in new_keys.items():
        assert on_disk[key] == value, key
    r = client.get('/api/preferences')
    assert r.status_code == 200
    data = r.get_json()
    assert data['globalServersOptionsEnabled'] is True
    assert data['defaultAuthentikCopyHeaders'].endswith('X-Authentik-Meta-Version')

    # Old preferences without the new keys still validate: missing keys fall
    # back to their defaults instead of breaking the endpoint.
    del on_disk['globalServersOptionsEnabled']
    on_disk['theme'] = 'theme-dark'
    Path(app_module.PREFERENCES_FILE).write_text(json.dumps(on_disk), encoding='utf-8')
    r = client.post('/api/preferences', json={'theme': 'theme-light-gray'},
                    headers={'X-CSRFToken': 'pref-new-csrf'})
    assert r.status_code == 200
    saved = r.get_json()['saved_prefs']
    assert saved['globalServersOptionsEnabled'] is False  # back to default
    assert saved['theme'] == 'theme-light-gray'
    # The other new keys survived on disk.
    assert saved['globalServersIdleTimeout'] == '10m'
    assert saved['siteTransportKeepAliveIdle'] == '5m'


# ---------------------------------------------------------------------------
# 6. Security fixes: secret placeholder, XFF rate-limit bypass, users.json
#    fail-safe mode, atomic writes
# ---------------------------------------------------------------------------

def test_placeholder_secret_keys_are_refused():
    """Known placeholders (repo docs) and short keys must be rejected."""
    for bad in ('replace-me-with-a-secure-key',
                'REPLACE-ME-WITH-A-SECURE-KEY',
                'dev-only-unsafe-default-key-3f9a1z-CHANGE-IN-PROD',
                'your_very_strong_secret_key_here',
                'your_strong_secret',
                None,                      # unset
                '',                        # empty
                'short-key'):              # < 32 chars
        reason = app_module.insecure_secret_key_reason(bad)
        assert reason, f'{bad!r} should be refused (no reason returned)'

    assert app_module.insecure_secret_key_reason('a' * 32) is None
    assert app_module.insecure_secret_key_reason('a-strong-random-key-0123456789abcdef') is None

    # The running app must never hold a publicly-known placeholder value.
    assert app.config['SECRET_KEY'] != 'replace-me-with-a-secure-key'
    assert app.config['SECRET_KEY'] != 'dev-only-unsafe-default-key-3f9a1z-CHANGE-IN-PROD'


def test_trusted_proxy_count_env_parsing(monkeypatch):
    """TRUSTED_PROXY_COUNT parsing: default 0, invalid values fall back to 0."""
    monkeypatch.setenv('TRUSTED_PROXY_COUNT', '')
    assert app_module._trusted_proxy_count_from_env() == 0
    monkeypatch.delenv('TRUSTED_PROXY_COUNT')
    assert app_module._trusted_proxy_count_from_env() == 0
    monkeypatch.setenv('TRUSTED_PROXY_COUNT', '2')
    assert app_module._trusted_proxy_count_from_env() == 2
    monkeypatch.setenv('TRUSTED_PROXY_COUNT', '-3')
    assert app_module._trusted_proxy_count_from_env() == 0
    monkeypatch.setenv('TRUSTED_PROXY_COUNT', 'not-a-number')
    assert app_module._trusted_proxy_count_from_env() == 0


def test_proxy_fix_disabled_in_direct_mode():
    """Direct mode (default): app.wsgi_app must NOT be wrapped by ProxyFix."""
    from werkzeug.middleware.proxy_fix import ProxyFix
    assert not isinstance(app.wsgi_app, ProxyFix)


def test_rate_limit_not_bypassable_via_forged_xff(client):
    """Direct mode: rotating forged X-Forwarded-For values must NOT reset the
    login rate-limit counter (all attempts count against the real IP)."""
    _create_admin()
    with client.session_transaction() as sess:
        sess['_csrf_token'] = 'xff-csrf'

    # 5 failures, each one claiming a different source IP via forged XFF
    for i in range(5):
        r = client.post('/login', data={'username': ADMIN_USER, 'password': 'wrong',
                                        'csrf_token': 'xff-csrf'},
                        headers={'X-Forwarded-For': f'203.0.113.{i + 1}'})
        assert 'Invalid username or password' in r.get_data(as_text=True), \
            f'attempt {i + 1} unexpectedly blocked early'

    # 6th attempt is still blocked although the forged XFF is yet another
    # never-seen IP: without the fix this would succeed (brute force).
    r = client.post('/login', data={'username': ADMIN_USER, 'password': ADMIN_PASS,
                                    'csrf_token': 'xff-csrf'},
                    headers={'X-Forwarded-For': '198.51.100.77'})
    assert 'Too many login attempts' in r.get_data(as_text=True)
    with client.session_transaction() as sess:
        assert sess.get('username') is None


def test_corrupted_users_json_setup_refused(client):
    """A truncated users.json must fail safe: /setup returns 500 (manual
    intervention required) instead of treating the file as empty."""
    users_file = Path(app.config['USERS_FILE'])
    users_file.parent.mkdir(parents=True, exist_ok=True)
    users_file.write_text('{"admin": {"password": "pbkdf2:sha256:15000',
                          encoding='utf-8')  # truncated mid-write

    r = client.get('/setup')
    assert r.status_code == 500, f'expected 500, got {r.status_code}'
    body = r.get_data(as_text=True).lower()
    assert 'corrupt' in body or 'intervention' in body

    # POST /setup is equally refused (no admin account can be injected)
    with client.session_transaction() as sess:
        sess['_csrf_token'] = 'corrupt-csrf'
    r = client.post('/setup', data={
        'username': 'evil', 'password': 'evilpass123',
        'confirm_password': 'evilpass123', 'csrf_token': 'corrupt-csrf'})
    assert r.status_code == 500

    # Login is fail-closed too: no redirect to the (refused) setup page
    r = client.post('/login', data={'username': ADMIN_USER, 'password': ADMIN_PASS,
                                    'csrf_token': 'corrupt-csrf'})
    assert r.status_code == 500

    # A file that is really ABSENT still allows the setup flow
    users_file.unlink()
    r = client.get('/setup')
    assert r.status_code == 200


def test_atomic_write_happy_path_and_mode(tmp_path):
    """atomic_write creates parent dirs, writes content and applies mode."""
    from fs_utils import atomic_write
    target = tmp_path / 'nested' / 'users.json'
    assert atomic_write(target, '{"a": 1}\n', mode=0o600) is True
    assert target.read_text(encoding='utf-8') == '{"a": 1}\n'
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_atomic_write_target_intact_when_write_fails(tmp_path, monkeypatch):
    """Simulated crash mid-write (fsync fails) leaves the old file untouched
    and no temp file behind."""
    from fs_utils import atomic_write
    target = tmp_path / 'data.json'
    target.write_text('ORIGINAL', encoding='utf-8')

    def _boom(fd):
        raise OSError('simulated disk failure')
    monkeypatch.setattr(os, 'fsync', _boom)

    assert atomic_write(target, 'NEW CONTENT') is False
    assert target.read_text(encoding='utf-8') == 'ORIGINAL'
    assert [p.name for p in tmp_path.iterdir()] == ['data.json']


def test_save_users_atomic_and_owner_only_permissions():
    """save_users goes through atomic_write and chmods users.json 600."""
    assert app_module.save_users({ADMIN_USER: {'password': 'hashed-value'}})
    users_path = Path(app.config['USERS_FILE'])
    saved = json.loads(users_path.read_text(encoding='utf-8'))
    assert saved[ADMIN_USER]['password'] == 'hashed-value'
    # Owner-only: no group/other permission bits set
    assert stat.S_IMODE(users_path.stat().st_mode) & 0o077 == 0


def test_configure_caddyfile_logging_uses_atomic_write(client, monkeypatch):
    """The parser's final write goes through atomic_write (regression)."""
    import caddyfile_parser

    caddyfile = Path(os.environ['CADDY_CONFIG'])
    caddyfile.write_text("example.com {\n    root * /var/www\n}\n", encoding='utf-8')

    class FakeResult:
        returncode = 0
        stdout = ''
        stderr = ''
    monkeypatch.setattr(app_module.subprocess, 'run', lambda *a, **k: FakeResult())

    result = caddyfile_parser.configure_caddyfile_logging(str(caddyfile))
    assert result['status'] == 'success'
    content = caddyfile.read_text(encoding='utf-8')
    assert 'format json' in content
    # No temp leftovers next to the Caddyfile
    leftovers = [p for p in caddyfile.parent.iterdir()
                 if p.name.startswith('.Caddyfile.') and p.name.endswith('.tmp')]
    assert leftovers == []


# ---------------------------------------------------------------------------
# 7. Follow-up hardening: proxy mode positive test, env secret honored,
#    setup save failure feedback, preferences via atomic_write, stale tmp files
# ---------------------------------------------------------------------------

def test_valid_env_secret_key_is_used_as_secret_key():
    """A valid FLASK_SECRET_KEY from the environment IS used as SECRET_KEY
    (not replaced by an ephemeral random value)."""
    env_secret = os.environ['FLASK_SECRET_KEY']
    assert app_module.insecure_secret_key_reason(env_secret) is None
    assert app.config['SECRET_KEY'] == env_secret


def test_proxy_mode_enables_proxy_fix_and_rewrites_remote_addr():
    """Positive proxy-mode check (subprocess re-import): TRUSTED_PROXY_COUNT=1
    wraps wsgi_app with ProxyFix(x_for=1) and the login rate limiter records
    the X-Forwarded-For-derived client IP instead of the TCP peer address."""
    repo_root = Path(__file__).resolve().parent.parent
    script = f'''
import json, os, sqlite3, sys, tempfile
tmp = tempfile.mkdtemp(prefix='caddypanel_proxyfix_')
os.environ.update({{
    'APP_DATA_DIR': tmp,
    'CADDY_CONFIG': os.path.join(tmp, 'Caddyfile'),
    'CADDY_ACCESS_LOG_FILE': os.path.join(tmp, 'caddy_access.json.log'),
    'GEOIP_DB_PATH': os.path.join(tmp, 'GeoLite2-Country.mmdb'),
    'FLASK_SECRET_KEY': 'proxy-test-secret-key-0123456789abcdef',
    'TRUSTED_PROXY_COUNT': '1',
}})
sys.path.insert(0, {str(repo_root)!r})
import app as app_module
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash

wrapped = isinstance(app_module.app.wsgi_app, ProxyFix)
x_for = getattr(app_module.app.wsgi_app, 'x_for', None)

# End-to-end proof: a failed login records the XFF-derived client IP.
users_file = app_module.USERS_FILE
users_file.write_text(json.dumps(
    {{'admin': {{'password': generate_password_hash('password123')}}}}))
client = app_module.app.test_client()
with client.session_transaction() as sess:
    sess['_csrf_token'] = 't'
client.post('/login', data={{'username': 'admin', 'password': 'wrong',
                            'csrf_token': 't'}},
            headers={{'X-Forwarded-For': '203.0.113.9'}})
conn = sqlite3.connect(os.path.join(tmp, 'stats.db'))
try:
    ips = [row[0] for row in conn.execute('SELECT ip FROM login_attempts')]
finally:
    conn.close()
print(json.dumps({{'proxy_fix': wrapped, 'x_for': x_for, 'ips': ips}}))
'''
    result = subprocess.run([sys.executable, '-c', script],
                            capture_output=True, text=True, timeout=120,
                            cwd=str(repo_root))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload['proxy_fix'] is True
    assert payload['x_for'] == 1
    # remote_addr was rewritten by ProxyFix before the rate limiter saw it
    assert payload['ips'] == ['203.0.113.9']


def test_setup_flash_error_when_save_users_fails(client, monkeypatch):
    """When save_users() fails, /setup re-renders with an explicit error flash
    and users.json is NOT created."""
    users_file = Path(app.config['USERS_FILE'])
    users_file.unlink(missing_ok=True)

    def _failing_save(users):
        return False
    monkeypatch.setattr(app_module, 'save_users', _failing_save)

    with client.session_transaction() as sess:
        sess['_csrf_token'] = 'setup-fail-csrf'
    r = client.post('/setup', data={
        'username': 'setupadmin', 'password': 'a-very-strong-password',
        'confirm_password': 'a-very-strong-password',
        'csrf_token': 'setup-fail-csrf'})
    # Form is re-rendered (no redirect), with the error flash shown
    assert r.status_code == 200
    assert 'Error saving admin account' in r.get_data(as_text=True)
    assert not users_file.exists(), 'users.json must not exist after failed save'


def test_save_preferences_goes_through_atomic_write_owner_only(client, monkeypatch):
    """preferences.json is persisted through atomic_write with owner-only
    permissions (it holds the MaxMind license key)."""
    recorded = {}
    real_atomic_write = app_module.atomic_write

    def spy_atomic_write(path, content, mode=None):
        recorded['path'] = Path(path)
        recorded['content'] = content
        recorded['mode'] = mode
        return real_atomic_write(path, content, mode=mode)

    monkeypatch.setattr(app_module, 'atomic_write', spy_atomic_write)

    _create_admin()
    with client.session_transaction() as sess:
        sess['username'] = ADMIN_USER
        sess['_csrf_token'] = 'prefs-aw-csrf'

    r = client.post('/api/preferences', json={'theme': 'theme-dark'},
                    headers={'X-CSRFToken': 'prefs-aw-csrf'})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert recorded['path'] == Path(app.config['PREFERENCES_FILE'])
    assert recorded['mode'] == 0o600
    saved = json.loads(recorded['content'])  # payload written is valid JSON
    assert saved['theme'] == 'theme-dark'
    prefs_mode = stat.S_IMODE(Path(app.config['PREFERENCES_FILE']).stat().st_mode)
    assert prefs_mode & 0o077 == 0


def test_readfile_blocks_stale_atomic_write_tmp_files(client):
    """Leftover atomic_write temp files ('.Caddyfile.xxxx.tmp', '.users.json.xxxx.tmp')
    are refused by /api/readfile even though their basename is not an exact
    SENSITIVE_FILES entry."""
    _create_admin()
    stale = app_module.FILE_BROWSE_DIR / '.Caddyfile.ab12cd34.tmp'
    stale.write_text('partial caddyfile content', encoding='utf-8')
    try:
        with client.session_transaction() as sess:
            sess['username'] = ADMIN_USER
        r = client.get(f'/api/readfile?path={stale.name}')
        assert r.status_code == 403, \
            f'expected 403, got {r.status_code}: {r.get_data(as_text=True)}'
    finally:
        stale.unlink(missing_ok=True)


def test_cleanup_stale_tmp_files_removes_leftovers_only():
    """cleanup_stale_tmp_files deletes only '.<name>.<rand>.tmp' leftovers."""
    from fs_utils import cleanup_stale_tmp_files
    data_dir = Path(os.environ['APP_DATA_DIR'])
    stale = data_dir / '.users.json.deadbeef.tmp'
    keeper = data_dir / 'not-a-tmp-file.txt'
    stale.write_text('{"partial": ', encoding='utf-8')
    keeper.write_text('keep me', encoding='utf-8')
    try:
        removed = cleanup_stale_tmp_files(data_dir)
        assert removed >= 1
        assert not stale.exists()
        assert keeper.exists(), 'non-tmp files must be left alone'
    finally:
        stale.unlink(missing_ok=True)
        keeper.unlink(missing_ok=True)
