# CaddyPanel

CaddyPanel is a simple, self-hosted web UI for managing a **Caddy v2** server. It is designed to run in a single, all-in-one Docker container, providing an easy way to configure reverse proxies and manage your Caddyfile without directly editing files on the command line.

This project is ideal for users who want a straightforward graphical interface for their Caddy instance, especially in home-server or small project environments.

> [!WARNING]
> **Development Warning**
> This project was developed 100% by an artificial intelligence (Google Gemini) under human supervision. While functional, it is important to keep this unique development method in mind when using, modifying, or evaluating the code.

## Features

- **All-in-one Docker Container**: Caddy and the Flask web UIe are managed by Supervisor within a single container.
- **Easy-to-use UI**: Manage your Caddy configurations with a simple table-based interface or a raw Caddyfile editor.
- **Multi-Arch Support**: The official Docker image supports both `linux/amd64` (standard PCs, servers) and `linux/arm64` (Raspberry Pi, etc.).
- **User Authentication**: A simple user system protects access to the panel. The first user to register becomes the administrator.
- **Persistent Configuration**: All your Caddyfiles, certificates, and user preferences are persisted through Docker volumes.
- **Automatic Caddy Reloads**: Caddy is automatically reloaded upon any configuration change, applying your updates instantly.

## Quick Start with Docker Compose

This is the recommended method for running CaddyPanel.

### 1. Prerequisites

- [Docker](https://docs.docker.com/get-docker/)
- [Docker Compose](https://docs.docker.com/compose/install/)

### 2. Create the `docker-compose.yml` file

Create a new file named `docker-compose.yml` and paste the following content into it:

```yaml
# ==================================================================================
#                   Example docker-compose file for CaddyPanel
#

#  How to use:
#  1. Copy this content into a file named `docker-compose.yml`.
 #  2. Edit the environment variables below, especially FLASK_SECRET_KEY.
#  3. Run `docker compose up -d` in the same directory.
#
# CaddyPanel will then be accessible at http://<your_server_ip>:5000 for initial setup.
# Once Caddy is configured to manage a domain, you will access CaddyPanel through that domain.
# ==================================================================================

version: '3.8'

services: 
  caddypanel:
    # Use the official image from Docker Hub.
    image: holaflenain/caddypanel:latest
    container_name: caddypanel
    restart: unless-stopped

    ports:
      # Standard ports for web traffic handled by Caddy.
      - "80:80"
      - "443:443"
      # Port for the CaddyPanel web interface (initial setup over plain HTTP).
      # SECURITY: once your admin account is created and the UI is reachable
      # through the domain managed by Caddy, close this port (comment out the
      # line below) so the panel is only served through Caddy with TLS.
      - "5000:5000"

    environment:
      # --- EDIT BELOW ---
      # Secret key to secure Flask sessions.
      #  !! VERY IMPORTANT !! Replace this placeholder with a long, random
      # string BEFORE first use (generate one with: openssl rand -hex 32).
      # THE PANEL WILL NOT WORK PROPERLY UNTIL YOU DO: placeholder/short
      # values (< 32 chars) are REFUSED at startup and each gunicorn worker
      # then generates its own ephemeral key -> login loop and random CSRF
      # 400 errors (an explicit error is logged at startup).
      - FLASK_SECRET_KEY=replace-me-with-a-secure-key

      # Reverse-proxy trust: leave unset (or 0) while accessing the panel
      # directly on port 5000. Once served through Caddy, set it to 1.
      # WARNING: keeping TRUSTED_PROXY_COUNT=0 behind Caddy/nginx shares ONE
      # login rate-limit bucket between ALL clients: anyone can lock /login
      # for everybody for 5 minutes with 5 bad attempts.
      # - TRUSTED_PROXY_COUNT=1
      
      # Session cookie security: the Secure flag is ON by default (HTTPS).
      # If you access the panel over plain HTTP (http://<ip>:5000) without a
      # TLS-terminating reverse proxy, set FLASK_COOKIE_SECURE=0:
      # - FLASK_COOKIE_SECURE=0
      
      # Timezone for Caddy and application logs to be accurate.
      # List of timezones: https://en.wikipedia.org/wiki/List_of_zz_database_time_zones
      - TZ=Etc/UTC

    volumes:
      # Volume for Caddy's configuration (Caddyfile).
      # The leading './' means the 'caddy_config' folder will be created
      # in the same directory as this docker-compose.yml file.
      - ./caddy_config:/etc/caddy
      
      # Volume for Caddy's data (SSL certificates, etc.).
      - ./caddy_data:/data
      
      # Volume for CaddyPanel's data (users, preferences).
      - ./caddypanel_data:/app_data
```

### 3. Configure the `FLASK_SECRET_KEY`

This step is **critical for security**. In your `docker-compose.yml` file, change the `FLASK_SECRET_KEY` to a unique, random string. You can generate a strong key with the following command:
```sh
openssl rand -hex 32
```
Copy the output and paste it as the value for `FLASK_SECRET_KEY`.

Placeholder values (such as `replace-me-with-a-secure-key`) and keys shorter
than 32 characters are **refused at startup**: an explicit error is logged and
an ephemeral random key is generated instead. Because the production
entrypoint runs gunicorn with several workers (`--workers 4`), each worker
generates its **own** ephemeral key: requests alternate randomly between
workers, so you get an endless login loop and random `400 CSRF` errors — the
panel is effectively **unusable** in that degraded state. Even with a single
process, sessions will not survive restarts. Set a strong key before use.

You should also set the `TZ` (timezone) variable to your local timezone.

*# 4. Start the Container

Navigate to the directory containing your `docker-compose.yml` file and run:
```sh
docker compose up -d
```
The container will now start in the background.

## First-Time Setup

1.  Open your web browser and navigate to `http://<your_server_ip>:5000`.
2.  You will be redirected to the setup page to create the first user account.
3.  **The first user to register automatically becomes the administrator.**
4.  Log in with your new credentials, and you can start configuring Caddy!

## Security & Environment Variables

### Reverse-proxy trust (`TRUSTED_PROXY_COUNT`)

By default CaddyPanel starts in **direct mode**: `X-Forwarded-*` headers sent
by clients are **ignored** and the client IP used by the login rate limiter is
the actual TCP connection address. This prevents an attacker with direct access
to port 5000 from forging a different `X-Forwarded-For` value on every attempt
to bypass rate limiting.

If — and only if — the panel is reached through a reverse proxy that
**overwrites** `X-Forwarded-For` (the bundled Caddy does), set the number of
trusted proxy hops so real client IPs are restored:

```sh
# docker-compose.yml
environment:
  - TRUSTED_PROXY_COUNT=1
```

> [!WARNING]
> Do **not** leave `TRUSTED_PROXY_COUNT=0` (direct mode) while serving the
> panel through the bundled Caddy (or any other reverse proxy): every request
> then appears to come from the proxy's own address (e.g. `127.0.0.1`), so all
> clients share **one single** login rate-limiting bucket. Anyone — including
> a remote attacker — can lock `/login` for **all users for 5 minutes** with
> just 5 failed attempts. Set `TRUSTED_PROXY_COUNT=1` behind the bundled Caddy.
> The active mode (and this trade-off) is explained in a startup log message.

> [!IMPORTANT]
> After the initial setup, close port 5000 (remove or comment out the
> `"5000:5000"` mapping in `docker-compose.yml`) and access the panel through
> the domain managed by Caddy. Port 5000 is plain HTTP and bypasses the reverse
> proxy entirely.

### Session cookie (`FLASK_COOKIE_SECURE`)

By default, CaddyPanel sets the `Secure` flag on the session cookie (it is only
sent over HTTPS). This is the recommended setting when the panel is served
behind Caddy with TLS, or through any TLS-terminating reverse proxy.

> [!IMPORTANT]
> If you access the panel **directly over plain HTTP** (e.g.
> `http://<your_server_ip>:5000` without a reverse proxy in front), the
> session cookie will **not** be sent and login will silently fail. In that
> case, set the environment variable `FLASK_COOKIE_SECURE=0` to allow the
> cookie over HTTP:
>
> ```sh
> # docker-compose.yml
> environment:
>   - FLASK_COOKIE_SECURE=0
> ```
>
> ```sh
> # docker run / plain gunicorn
> FLASK_COOKIE_SECURE=0 gunicorn --workers 4 app:app
> ```
>
> Once you serve the panel through HTTPS (Caddy manages a domain), remove this
> variable (or set it back to `1`) so the cookie is `Secure` again.

### Debug mode (`FLASK_DEBUG`)

When the Flask dev server is started directly (`python app.py`), debug mode is
**off** by default. To enable it for development, set `FLASK_DEBUG=1`:

```sh
FLASK_DEBUG=1 python app.py
```

Debug mode is never used by the production (gunicorn) entrypoint.

## Long-lived Connections & Keep-Alive (WebSockets / SSE)

CaddyPanel can harden proxy connections for streaming workloads. Defaults live in
`DEFAULT_PREFERENCES` (`app.py`) and are editable in **Preferences → "Long-lived Connections & Keep-Alive"**:

| Preference | Default | Role |
|---|---|---|
| `globalServersOptionsEnabled` | `false` | Manage the hardened `servers` block in the Caddyfile global options |
| `globalServersIdleTimeout` | `10m` | Global `servers { timeouts { idle … } }` value |
| `globalKeepAliveInterval` | `30s` | Global `servers { keepalive_interval … }` value |
| `siteFlushIntervalEnabled` | `true` | Inject `flush_interval -1` in generated/hardened `reverse_proxy` blocks |
| `siteTransportKeepAliveIdle` | `5m` | `transport http { keepalive_idle … }` for generated/hardened sites |
| `siteTransportKeepAliveInterval` | `30s` | `transport http { keepalive_interval … }` for generated/hardened sites |

Endpoints (POST, JSON body, session + CSRF token required). Existing different values on disk are **never overwritten**:

-   `POST /api/caddyfile/configure_servers_options` — body `{"idleTimeout": "10m", "keepAliveInterval": "30s"}` (optional, falls back to the saved preferences). Ensures the global block carries `servers { timeouts { idle … } keepalive_interval … }`. Exposed in the UI as the **"Apply to Caddyfile"** button.
-   `POST /api/caddyfile/harden_site` — body `{"upstream": "http://app:8080", "flush": true, "keepAliveIdle": "5m", "keepAliveInterval": "30s"}` (`upstream` required, others optional). Hardens every matching `reverse_proxy <upstream>` directive. Exposed in the UI as the per-site 🛡️ **Harden** action.

Possible parser statuses returned as `parser_status`: `created`, `updated`, `unchanged`, `conflict`, `not_found`, `error`
(HTTP 200 except `not_found`/invalid parameters → 400 and write/parser failures → 500).

## Volumes Explained

CaddyPanel uses three volumes to persist data. By using relative paths (`./`), these folders will be created in the same directory where you run `docker compose up`.

-   `./caddy_config` : Stores your `Caddyfile`. This is the main configuration file for the Caddy server.
-   `./caddy_data` : Stores Caddy's operational data, most importantly the SSL certificates it obtains from Let's Encrypt.
-   `./caddypanel_dat`` : Stores CaddyPanel's application data, such as user accounts and panel preferences.

Backing up these three folders is all you need to do to save your entire CaddyPanel setup.
