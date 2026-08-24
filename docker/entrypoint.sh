#!/bin/bash
set -e # Exit immediately if a command exits with a non-zero status.

echo ">>> Running entrypoint.sh as $(whoami)"

# Use the environment variables defined in the Dockerfile
# APP_DATA_DIR, CADDY_CONFIG_FILE, FLASK_APP_DIR are already env variables.

# Paths of the default source files IN THE IMAGE for initialization
DEFAULT_PREFS_PATH="${FLASK_APP_DIR}/preferences.json"
DEFAULT_USERS_PATH="${FLASK_APP_DIR}/users.json" # Example users.json file (if it exists)
DEFAULT_CADDYFILE_EXAMPLE_PATH="${FLASK_APP_DIR}/caddyfile/Caddyfile" # Provided example Caddyfile

# Target paths in the VOLUMES
TARGET_PREFS_FILE="${APP_DATA_DIR}/preferences.json"
TARGET_USERS_FILE="${APP_DATA_DIR}/users.json"
# CADDY_CONFIG_FILE is already the target path (e.g., /etc/caddy/Caddyfile)

# Create the data directories if the user has mounted them and they do not exist yet
# Docker creates the mount points, but not necessarily the subfolders if the volume is an empty existing folder.
mkdir -p "${APP_DATA_DIR}"
mkdir -p "${CADDY_CONFIG_DIR}" # e.g., /etc/caddy
mkdir -p "${CADDY_DATA_DIR}"   # e.g., /data/caddy
mkdir -p /var/log/caddy_panel   # for JSON access logs (persisted via volume)

# --- Download GeoLite2-Country database if credentials are set ---
GEOIP_DB_PATH="${APP_DATA_DIR}/GeoLite2-Country.mmdb"
if [ -f "$GEOIP_DB_PATH" ]; then
    echo "GeoIP: database already exists at $GEOIP_DB_PATH"
elif [ -n "$MAXMIND_ACCOUNT_ID" ] && [ -n "$MAXMIND_LICENSE_KEY" ]; then
    # Strategy 1: use geoipupdate if available (most reliable)
    if command -v geoipupdate >/dev/null 2>&1; then
        echo "GeoIP: downloading via geoipupdate..."
        cat >"${APP_DATA_DIR}/GeoIP.conf" <<GEOIPCONF
AccountID ${MAXMIND_ACCOUNT_ID}
LicenseKey ${MAXMIND_LICENSE_KEY}
EditionIDs GeoLite2-Country
DatabaseDirectory ${APP_DATA_DIR}
GEOIPCONF
        # GeoIP.conf contains the MaxMind LicenseKey in clear text:
        # restrict permissions during its brief lifetime.
        chmod 600 "${APP_DATA_DIR}/GeoIP.conf"
        if geoipupdate -f "${APP_DATA_DIR}/GeoIP.conf" -d "${APP_DATA_DIR}"; then
            if [ -f "$GEOIP_DB_PATH" ]; then
                echo "GeoIP: database downloaded via geoipupdate to $GEOIP_DB_PATH"
            else
                echo "WARNING: geoipupdate ran but $GEOIP_DB_PATH not found. Falling back to HTTP download."
            fi
        else
            echo "WARNING: geoipupdate failed. Falling back to HTTP download."
        fi
        # Purge the credentials file whether geoipupdate succeeded or failed.
        rm -f "${APP_DATA_DIR}/GeoIP.conf"
    fi
    # Strategy 2: if geoipupdate didn't work or isn't available, try HTTP
    if [ ! -f "$GEOIP_DB_PATH" ]; then
        echo "GeoIP: trying HTTP download from MaxMind API..."
        if curl -fsSL -u "${MAXMIND_ACCOUNT_ID}:${MAXMIND_LICENSE_KEY}" \
            "https://download.maxmind.com/app/geoip_download?edition_id=GeoLite2-Country&suffix=tar.gz" \
            -o /tmp/GeoLite2-Country.tar.gz 2>/dev/null; then
            cd /tmp && tar xzf GeoLite2-Country.tar.gz && mv GeoLite2-Country_*/GeoLite2-Country.mmdb "$GEOIP_DB_PATH" 2>/dev/null
            rm -f /tmp/GeoLite2-Country.tar.gz /tmp/GeoLite2-Country_* 2>/dev/null
            cd "${FLASK_APP_DIR}"
            if [ -f "$GEOIP_DB_PATH" ]; then
                echo "GeoIP: database downloaded via HTTP to $GEOIP_DB_PATH"
            else
                echo "WARNING: GeoIP HTTP download failed. Country stats will be unavailable."
                echo "       Try downloading manually or check https://www.maxmind.com/en/geolite2/eula"
            fi
        else
            echo "WARNING: Could not download GeoLite2-Country database. Check credentials."
            echo "       You can also download it manually from https://www.maxmind.com and place it at $GEOIP_DB_PATH"
        fi
    fi
else
    echo "GeoIP: MAXMIND_ACCOUNT_ID and/or MAXMIND_LICENSE_KEY not set. Country statistics will be unavailable."
    echo "       Get a free account at https://www.maxmind.com/en/geolite2/signup"
    echo "       Then enter the credentials in the Preferences page (GeoIP section)."
fi
export GEOIP_DB_PATH

# Initialize preferences.json
if [ ! -f "$TARGET_PREFS_FILE" ]; then
    echo "Initializing preferences.json at $TARGET_PREFS_FILE..."
    if [ -f "$DEFAULT_PREFS_PATH" ]; then
        cp "$DEFAULT_PREFS_PATH" "$TARGET_PREFS_FILE"
        # Ensure that caddyfilePath points to the internal path in the container
        # Using sed (beware of special characters in paths)
        # Replace the value of caddyfilePath with the environment variable CADDY_CONFIG_FILE
        # Escape the slashes for sed:
        ESCAPED_CADDY_CONFIG_FILE=$(echo "$CADDY_CONFIG_FILE" | sed 's/\//\\\//g')
        sed -i.bak "s|\"caddyfilePath\": \".*\"|\"caddyfilePath\": \"${ESCAPED_CADDY_CONFIG_FILE}\"|g" "$TARGET_PREFS_FILE"
        rm -f "${TARGET_PREFS_FILE}.bak"
        echo "preferences.json initialized and paths updated."
    else
        echo "WARNING: Default preferences.json not found at $DEFAULT_PREFS_PATH. Creating a minimal one."
        # Create a minimal preferences file if the default file is not found
        echo "{}" > "$TARGET_PREFS_FILE" # app.py will fill it with defaults on first load
    fi
else
    echo "preferences.json already exists at $TARGET_PREFS_FILE."
    # Optional: Check and update caddyfilePath if it is incorrect, even if the file exists.
    # This can be useful if the user has mounted an old preferences.json.
    # jq would be more robust for this if available:
    # if command -v jq &> /dev/null;
    #   jq --arg path "$CADDY_CONFIG_FILE" '.caddyfilePath = $path' "$TARGET_PREFS_FILE" > tmp.$$.json && mv tmp.$$.json "$TARGET_PREFS_FILE"
    # else
    #   echo "jq not installed, skipping explicit update of existing preferences.json paths. app.py should handle it."
    # fi
fi

# Initialize users.json (will be empty, the setup will take care of it, or copied from the example)
if [ ! -f "$TARGET_USERS_FILE" ]; then
    echo "Initializing users.json at $TARGET_USERS_FILE..."
    if [ -f "$DEFAULT_USERS_PATH" ] && [ -s "$DEFAULT_USERS_PATH" ]; then
        cp "$DEFAULT_USERS_PATH" "$TARGET_USERS_FILE"
        echo "users.json initialized from example."
    else
        echo "{}" > "$TARGET_USERS_FILE" # Will be filled by the setup page
        echo "Empty users.json created. Admin setup will be required."
    fi
else
    echo "users.json already exists at $TARGET_USERS_FILE."
fi

# Initialize Caddyfile
if [ ! -f "$CADDY_CONFIG_FILE" ] || [ ! -s "$CADDY_CONFIG_FILE" ]; then # If it does not exist or is empty
    echo "Initializing default Caddyfile at $CADDY_CONFIG_FILE..."
    if [ -f "$DEFAULT_CADDYFILE_EXAMPLE_PATH" ]; then
        cp "$DEFAULT_CADDYFILE_EXAMPLE_PATH" "$CADDY_CONFIG_FILE"
        echo "Caddyfile initialized from project example."
    else
        # Create a basic Caddyfile
        echo -e "{\n\t# admin admin@example.com\n\thttp_port 80\n\thttps_port 443\n\t# acme_dns <your_dns_provider> <api_token>\n\t# The email for ACME can also be configured via the UI preferences.\n}\n\n# Add your sites here\n# Example:\n# yourdomain.com {\n#    reverse_proxy your_internal_service:port\n# }" > "$CADDY_CONFIG_FILE"
        echo "Minimal default Caddyfile created."
    fi
else
    echo "Caddyfile already exists at $CADDY_CONFIG_FILE."
fi

# --- Auto-configure JSON logging in Caddyfile ---
# Ensure the global block has a 'log { output stdout format json }' directive
# AND every site block has a 'log' directive, so Caddy actually emits access logs.
if grep -q 'format json' "$CADDY_CONFIG_FILE" 2>/dev/null; then
    echo "Caddyfile already has JSON logging configured."
else
    echo "Adding JSON logging configuration to Caddyfile global block..."
fi
# Always run the full configuration (add global log + add log to site blocks)
python3 -c "
import sys; sys.path.insert(0, '${FLASK_APP_DIR}')
from caddyfile_parser import configure_caddyfile_logging
result = configure_caddyfile_logging('${CADDY_CONFIG_FILE}')
print(result.get('message', 'Done.'))
"

# Ensure that the permissions are correct for the data directories mounted as volumes
# This is important because the user in the container (appuser) must be able to write.
# The effective owner on the host will not change, but the permissions IN THE CONTAINER will be correct.
# Warning: if files already exist and are owned by root on the host, 'appuser' will not be able to write
# unless the group/other permissions allow it or we chown here.
# `gosu` or `sudo` are not installed by default.
# `chown` will only work if entrypoint is executed as root. So we will chown here.
# Execute chown as root (the entrypoint is launched as root by default by Docker before the USER of the Dockerfile)
# Or, if the entrypoint is launched by `appuser`, it will not be able to chown.
# Solution: Docker executes ENTRYPOINT *before* changing user with the USER directive,
# so the entrypoint runs as root, which allows chown.
# Unless the ENTRYPOINT is an exec form (json array), in which case it respects USER.
# If ENTRYPOINT is shell form (string), it is wrapped by /bin/sh -c, which runs as root.
# Our CMD is `supervisord`, which will be launched by `appuser` if USER is defined before CMD.
# The ENTRYPOINT `bash /entrypoint.sh` will be executed by root.

echo "Setting permissions for data directories..."
chown -R appuser:appgroup "${APP_DATA_DIR}" "${CADDY_DATA_DIR}" "${CADDY_CONFIG_DIR}" /var/log/caddy_panel
# The supervisor log is already managed in the Dockerfile.

echo "Entrypoint script finished. Handing over to CMD: $@"
# Execute the command passed to the script (CMD of the Dockerfile, e.g., supervisord)
# exec "$@" # If supervisord is launched by appuser directly
# If supervisord must be launched by root (to manage root processes), it must be launched differently.
# Here, CMD is ["supervisord", ...], which will be executed by the 'appuser' user because USER is defined before CMD.
# Supervisor itself runs as 'appuser', but can launch programs as other users if configured.
# In our supervisord.conf, we have set user=root for caddy, so supervisor (even if it runs as appuser)
# will try to launch caddy as root. This requires supervisor to be launched as root.
# So, in supervisord.conf, `user=root` for supervisor itself.
# And in Dockerfile, do not do `USER appuser` before CMD if supervisord must be root.

# If supervisord.conf has `user=root` for [supervisord] block:
exec "$@"
# If supervisord must run as appuser (which is safer if it does not need root):
# exec gosu appuser "$@" # Would require installing gosu