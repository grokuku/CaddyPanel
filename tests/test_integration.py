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

These tests import the real app with a throwaway APP_DATA_DIR / CADDY_CONFIG
so they never touch the repository's real stats.db or Caddyfile.
"""

import json
import os
import sqlite3
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
