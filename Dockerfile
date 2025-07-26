# --- Stage 1: Builder pour Caddy ---
FROM python:3.10-slim-bullseye AS caddy_builder

ARG CADDY_VERSION=2.10.0
# TARGETARCH est fourni automatiquement par Docker Buildx (ex: amd64, arm64)
ARG TARGETARCH

# Installer curl pour télécharger Caddy
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Télécharger et extraire Caddy
RUN \
    build_arch="${TARGETARCH:-$(dpkg --print-architecture)}" && \
    echo "Building for architecture: ${build_arch}" && \
    case "${build_arch}" in \
        amd64) caddy_arch_suffix='amd64';; \
        arm64) caddy_arch_suffix='arm64';; \
        armhf | arm/v6) caddy_arch_suffix='armv6';; \
        armel | arm/v5) caddy_arch_suffix='armv5';; \
        armv7 | arm/v7) caddy_arch_suffix='armv7';; \
        i386) caddy_arch_suffix='386';; \
        *) echo "ERROR: Unsupported architecture for Caddy: '${build_arch}'" >&2; exit 1;; \
    esac && \
    echo "Determined Caddy architecture suffix: ${caddy_arch_suffix}" && \
    curl -fsSL "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/caddy_${CADDY_VERSION}_linux_${caddy_arch_suffix}.tar.gz" -o /tmp/caddy.tar.gz && \
    tar -C /usr/local/bin -xzf /tmp/caddy.tar.gz caddy && \
    rm /tmp/caddy.tar.gz && \
    chmod +x /usr/local/bin/caddy


# --- Stage 2: Application finale ---
FROM python:3.10-slim-bullseye

# Arguments pour la création de l'utilisateur et du groupe
ARG APP_USER_ID=1000
ARG APP_GROUP_ID=1000

# Variables d'environnement
ENV CADDY_CONFIG_DIR=/etc/caddy
ENV CADDY_CONFIG_FILE=${CADDY_CONFIG_DIR}/Caddyfile
ENV CADDY_DATA_DIR=/data/caddy
ENV APP_DATA_DIR=/app_data
ENV FLASK_APP_DIR=/app
ENV FLASK_PORT=5000
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=off
ENV PIP_DISABLE_PIP_VERSION_CHECK=on

# Installer les dépendances système
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        supervisor \
    && apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Créer le groupe et l'utilisateur
RUN groupadd --gid ${APP_GROUP_ID} appgroup && \
    useradd --uid ${APP_USER_ID} --gid ${APP_GROUP_ID} --create-home --shell /bin/bash appuser

# Copier le binaire Caddy depuis le stage builder
COPY --from=caddy_builder /usr/local/bin/caddy /usr/bin/caddy

# Définir le répertoire de travail
WORKDIR ${FLASK_APP_DIR}

# Copier requirements.txt et installer les dépendances Python
# Ces fichiers sont maintenant à la racine du contexte de build (qui est C2RPM/)
COPY requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

# Copier le reste de l'application dans ${FLASK_APP_DIR}
# Le '.' signifie la racine du contexte de build (C2RPM/)
# Cela copiera app.py, static/, templates/, docker/, caddyfile/, etc. dans /app
COPY . ./

# Copier les configurations de Docker aux emplacements corrects
# Les fichiers sources sont maintenant relatifs au contexte de build (C2RPM/)
# donc docker/supervisord.conf fait référence à C2RPM/docker/supervisord.conf
COPY docker/supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Créer le répertoire de log pour supervisor
RUN mkdir -p /var/log/supervisor && \
    chown -R appuser:appgroup /var/log/supervisor

VOLUME ${CADDY_DATA_DIR} ${APP_DATA_DIR} ${CADDY_CONFIG_DIR}

EXPOSE 80 443 ${FLASK_PORT}

ENTRYPOINT ["/entrypoint.sh"]
CMD ["supervisord", "-n", "-c", "/etc/supervisor/conf.d/supervisord.conf"]