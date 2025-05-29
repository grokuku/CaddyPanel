#!/bin/bash
set -e # Exit immediately if a command exits with a non-zero status.

echo ">>> Running entrypoint.sh as $(whoami)"

# Utiliser les variables d'environnement définies dans le Dockerfile
# APP_DATA_DIR, CADDY_CONFIG_FILE, FLASK_APP_DIR sont déjà des variables d'env.

# Chemins des fichiers sources par défaut DANS L'IMAGE pour l'initialisation
DEFAULT_PREFS_PATH="${FLASK_APP_DIR}/preferences.json"
DEFAULT_USERS_PATH="${FLASK_APP_DIR}/users.json" # Fichier users.json d'exemple (s'il existe)
DEFAULT_CADDYFILE_EXAMPLE_PATH="${FLASK_APP_DIR}/caddyfile/Caddyfile" # Caddyfile d'exemple fourni

# Chemins cibles dans les VOLUMES
TARGET_PREFS_FILE="${APP_DATA_DIR}/preferences.json"
TARGET_USERS_FILE="${APP_DATA_DIR}/users.json"
# CADDY_CONFIG_FILE est déjà le chemin cible (ex: /etc/caddy/Caddyfile)

# Créer les répertoires de données si l'utilisateur les a montés et qu'ils n'existent pas encore
# Docker crée les points de montage, mais pas nécessairement les sous-dossiers si le volume est un dossier existant vide.
mkdir -p "${APP_DATA_DIR}"
mkdir -p "${CADDY_CONFIG_DIR}" # ex: /etc/caddy
mkdir -p "${CADDY_DATA_DIR}"   # ex: /data/caddy

# Initialiser preferences.json
if [ ! -f "$TARGET_PREFS_FILE" ]; then
    echo "Initializing preferences.json at $TARGET_PREFS_FILE..."
    if [ -f "$DEFAULT_PREFS_PATH" ]; then
        cp "$DEFAULT_PREFS_PATH" "$TARGET_PREFS_FILE"
        # S'assurer que caddyfilePath pointe vers le chemin interne au conteneur
        # Utilisation de sed (attention aux caractères spéciaux dans les chemins)
        # Remplacer la valeur de caddyfilePath par la variable d'environnement CADDY_CONFIG_FILE
        # Échapper les slashes pour sed:
        ESCAPED_CADDY_CONFIG_FILE=$(echo "$CADDY_CONFIG_FILE" | sed 's/\//\\\//g')
        sed -i.bak "s|\"caddyfilePath\": \".*\"|\"caddyfilePath\": \"${ESCAPED_CADDY_CONFIG_FILE}\"|g" "$TARGET_PREFS_FILE"
        # S'assurer que caddyReloadCmd est correct pour l'environnement conteneur
        ESCAPED_RELOAD_CMD="caddy reload --config ${CADDY_CONFIG_FILE} --adapter caddyfile"
        ESCAPED_RELOAD_CMD_SED=$(echo "$ESCAPED_RELOAD_CMD" | sed 's/\//\\\//g') # Échapper pour sed
        sed -i.bak "s|\"caddyReloadCmd\": \".*\"|\"caddyReloadCmd\": \"${ESCAPED_RELOAD_CMD_SED}\"|g" "$TARGET_PREFS_FILE"
        rm -f "${TARGET_PREFS_FILE}.bak"
        echo "preferences.json initialized and paths updated."
    else
        echo "WARNING: Default preferences.json not found at $DEFAULT_PREFS_PATH. Creating a minimal one."
        # Créer un fichier de préférences minimal si le fichier par défaut n'est pas trouvé
        echo "{}" > "$TARGET_PREFS_FILE" # app.py le remplira avec les défauts au premier chargement
    fi
else
    echo "preferences.json already exists at $TARGET_PREFS_FILE."
    # Optionnel: Vérifier et mettre à jour caddyfilePath et caddyReloadCmd s'ils sont incorrects, même si le fichier existe.
    # Cela peut être utile si l'utilisateur a monté un ancien preferences.json.
    # jq serait plus robuste pour cela si disponible:
    # if command -v jq &> /dev/null; then
    #   jq --arg path "$CADDY_CONFIG_FILE" '.caddyfilePath = $path' "$TARGET_PREFS_FILE" > tmp.$$.json && mv tmp.$$.json "$TARGET_PREFS_FILE"
    #   jq --arg cmd "caddy reload --config $CADDY_CONFIG_FILE --adapter caddyfile" '.caddyReloadCmd = $cmd' "$TARGET_PREFS_FILE" > tmp.$$.json && mv tmp.$$.json "$TARGET_PREFS_FILE"
    # else
    #   echo "jq not installed, skipping explicit update of existing preferences.json paths. app.py should handle it."
    # fi
fi

# Initialiser users.json (sera vide, le setup s'en chargera, ou copié depuis l'exemple)
if [ ! -f "$TARGET_USERS_FILE" ]; then
    echo "Initializing users.json at $TARGET_USERS_FILE..."
    if [ -f "$DEFAULT_USERS_PATH" ] && [ "$(jq 'length' "$DEFAULT_USERS_PATH" 2>/dev/null)" != "0" ]; then # Copier si non vide et JSON valide
        cp "$DEFAULT_USERS_PATH" "$TARGET_USERS_FILE"
        echo "users.json initialized from example."
    else
        echo "{}" > "$TARGET_USERS_FILE" # Sera rempli par la page de setup
        echo "Empty users.json created. Admin setup will be required."
    fi
else
    echo "users.json already exists at $TARGET_USERS_FILE."
fi

# Initialiser Caddyfile
if [ ! -f "$CADDY_CONFIG_FILE" ] || [ ! -s "$CADDY_CONFIG_FILE" ]; then # Si n'existe pas ou est vide
    echo "Initializing default Caddyfile at $CADDY_CONFIG_FILE..."
    if [ -f "$DEFAULT_CADDYFILE_EXAMPLE_PATH" ]; then
        cp "$DEFAULT_CADDYFILE_EXAMPLE_PATH" "$CADDY_CONFIG_FILE"
        echo "Caddyfile initialized from project example."
    else
        # Créer un Caddyfile de base
        echo -e "{\n\t# admin admin@example.com\n\thttp_port 80\n\thttps_port 443\n\t# acme_dns <your_dns_provider> <api_token>\n\t# L'email pour ACME peut aussi être configuré via les préférences de l'UI.\n}\n\n# Add your sites here\n# Example:\n# yourdomain.com {\n#    reverse_proxy your_internal_service:port\n# }" > "$CADDY_CONFIG_FILE"
        echo "Minimal default Caddyfile created."
    fi
else
    echo "Caddyfile already exists at $CADDY_CONFIG_FILE."
fi

# S'assurer que les permissions sont correctes pour les répertoires de données montés en volume
# Cela est important car l'utilisateur dans le conteneur (appuser) doit pouvoir écrire.
# Le propriétaire effectif sur l'hôte ne changera pas, mais les permissions DANS LE CONTENEUR seront correctes.
# Attention : si des fichiers existent déjà et sont possédés par root sur l'hôte, 'appuser' ne pourra pas écrire
# à moins que les permissions de groupe/autres le permettent ou que l'on chown ici.
# `gosu` ou `sudo` ne sont pas installés par défaut.
# `chown` ne fonctionnera que si entrypoint est exécuté en root. On va donc chowner ici.
# Exécuter chown en tant que root (l'entrypoint est lancé en root par défaut par Docker avant le USER du Dockerfile)
# Ou, si l'entrypoint est lancé par `appuser`, il ne pourra pas chowner.
# Solution : Docker exécute ENTRYPOINT *avant* de changer d'utilisateur avec la directive USER,
# donc l'entrypoint s'exécute en root, ce qui permet de chowner.
# Sauf si l'ENTRYPOINT est un exec form (json array), auquel cas il respecte USER.
# Si ENTRYPOINT est shell form (string), il est wrappé par /bin/sh -c, qui s'exécute en root.
# Notre CMD est `supervisord`, qui sera lancé par `appuser` si USER est défini avant CMD.
# L'ENTRYPOINT `bash /entrypoint.sh` sera exécuté par root.

echo "Setting permissions for data directories..."
chown -R appuser:appgroup "${APP_DATA_DIR}" "${CADDY_DATA_DIR}" "${CADDY_CONFIG_DIR}"
# Le log de supervisor est déjà géré dans le Dockerfile.

echo "Entrypoint script finished. Handing over to CMD: $@"
# Exécuter la commande passée au script (CMD du Dockerfile, ex: supervisord)
# exec "$@" # Si supervisord est lancé par appuser directement
# Si supervisord doit être lancé par root (pour gérer des process root), il faut le lancer différemment.
# Ici, CMD est ["supervisord", ...], qui sera exécuté par l'utilisateur 'appuser' car USER est défini avant CMD.
# Supervisor lui-même s'exécute en 'appuser', mais peut lancer des programmes en tant qu'autres users si configuré.
# Dans notre supervisord.conf, on a mis user=root pour caddy, donc supervisor (même s'il tourne en appuser)
# essaiera de lancer caddy en root. Cela nécessite que supervisor soit lancé en root.
# Donc, dans supervisord.conf, `user=root` pour supervisor lui-même.
# Et dans Dockerfile, ne pas faire `USER appuser` avant CMD si supervisord doit être root.

# Si supervisord.conf a `user=root` pour [supervisord] block:
exec "$@"
# Si supervisord doit s'exécuter en tant que appuser (ce qui est plus sûr s'il n'a pas besoin de root):
# exec gosu appuser "$@" # Nécessiterait d'installer gosu