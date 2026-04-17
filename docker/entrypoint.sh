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
        # Ensure that caddyReloadCmd is correct for the container environment
        ESCAPED_RELOAD_CMD="caddy reload --config ${CADDY_CONFIG_FILE} --adapter caddyfile"
        ESCAPED_RELOAD_CMD_SED=$(echo "$ESCAPED_RELOAD_CMD" | sed 's/\//\\\//g') # Escape for sed
        sed -i.bak "s|\"caddyReloadCmd\": \".*\"|\"caddyReloadCmd\": \"${ESCAPED_RELOAD_CMD_SED}\"|g" "$TARGET_PREFS_FILE"
        rm -f "${TARGET_PREFS_FILE}.bak"
        echo "preferences.json initialized and paths updated."
    else
        echo "WARNING: Default preferences.json not found at $DEFAULT_PREFS_PATH. Creating a minimal one."
        # Create a minimal preferences file if the default file is not found
        echo "{}" > "$TARGET_PREFS_FILE" # app.py will fill it with defaults on first load
    fi
else
    echo "preferences.json already exists at $TARGET_PREFS_FILE."
    # Optional: Check and update caddyfilePath and caddyReloadCmd if they are incorrect, even if the file exists.
    # This can be useful if the user has mounted an old preferences.json.
    # jq would be more robust for this if available:
    # if command -v jq &> /dev/null;
    #   jq --arg path "$CADDY_CONFIG_FILE" '.caddyfilePath = $path' "$TARGET_PREFS_FILE" > tmp.$$.json && mv tmp.$$.json "$TARGET_PREFS_FILE"
    #   jq --arg cmd "caddy reload --config $CADDY_CONFIG_FILE --adapter caddyfile" '.caddyReloadCmd = $cmd' "$TARGET_PREFS_FILE" > tmp.$$.json && mv tmp.$$.json "$TARGET_PREFS_FILE"
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
python3 << 'PYEOF'
import os, sys, re

path = os.environ['CADDY_CONFIG_FILE']


def find_matching_brace(content, start):
    if start >= len(content) or content[start] != '{':
        return -1
    depth = 0
    i = start
    in_string = False
    in_comment = False
    while i < len(content):
        ch = content[i]
        if in_comment:
            if ch == '\n': in_comment = False
            i += 1; continue
        if in_string:
            if ch == '\\': i += 2; continue
            if ch == '"': in_string = False
            i += 1; continue
        if ch == '#': in_comment = True; i += 1; continue
        if ch == '"': in_string = True; i += 1; continue
        if ch == '{': depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def add_log_to_site_blocks(content, global_start, global_end):
    """Add 'log' to every site block that lacks it."""
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
        close = find_matching_brace(content, idx)
        if close == -1:
            pos = idx + 1
            continue
        blocks.append((idx, close))
        pos = close + 1

    for block_open, block_close in reversed(blocks):
        block_body = content[block_open + 1:block_close]
        if re.search(r'^\s*log\b', block_body, re.MULTILINE):
            continue
        indent_match = re.search(r'\n(\s+)\S', block_body)
        indent = indent_match.group(1) if indent_match else '\t'
        insert = '\n' + indent + 'log'
        content = content[:block_open + 1] + insert + content[block_open + 1:]
    return content


try:
    content = open(path, 'r').read()
    desired = '\tlog {\n\t\toutput stdout\n\t\tformat json\n\t\tlevel INFO\n\t}'

    # Step 1: Add/replace global log block
    global_start = None
    global_end = None
    for i, ch in enumerate(content):
        if ch not in (' ', '\t', '\n', '\r'):
            if ch == '{':
                global_start = i
                global_end = find_matching_brace(content, i)
            break

    if global_start is not None and global_end is not None:
        inner = content[global_start + 1:global_end]
        # Remove any existing log block from the global block
        log_pattern = re.compile(r'^\s*log\s*\{', re.MULTILINE)
        log_match = log_pattern.search(inner)
        if log_match:
            brace_start = inner.index('{', log_match.start())
            brace_end = find_matching_brace(inner, brace_start)
            if brace_end != -1:
                line_start = inner.rfind('\n', 0, log_match.start()) + 1
                line_end = inner.find('\n', brace_end)
                if line_end == -1:
                    line_end = len(inner)
                else:
                    line_end += 1
                inner = inner[:line_start] + inner[line_end:]

        new_inner = inner.rstrip() + '\n' + desired + '\n'
        content = content[:global_start + 1] + new_inner + content[global_end:]
        # Recompute global_end after modification
        global_end = find_matching_brace(content, global_start)
    else:
        # No global block: create one
        global_block = '{\n' + desired + '\n}\n\n'
        content = global_block + content
        # Recompute
        for i, ch in enumerate(content):
            if ch not in (' ', '\t', '\n', '\r'):
                if ch == '{':
                    global_start = i
                    global_end = find_matching_brace(content, i)
                break

    # Step 2: Add log to every site block
    content = add_log_to_site_blocks(content, global_start, global_end)

    open(path, 'w').write(content)
    print('JSON logging configured (global block + site blocks).')
except Exception as e:
    print(f'Warning: could not auto-configure logging: {e}', file=sys.stderr)
    # Non-fatal: the user can configure logging via the UI later
PYEOF

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