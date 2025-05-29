# Plan de Transformation et Évolution de CaddyPanel

Ce document décrit le plan de transformation du projet CaddyPanel en une application conteneurisée intégrant Caddy2 et son interface de configuration Flask, ainsi que les évolutions futures envisagées.

## Objectif Principal de la Transformation Initiale

Transformer le projet existant en un **conteneur Docker unique** qui inclut :
1.  Le serveur web **Caddy 2**.
2.  L'**interface web de configuration Flask** (CaddyPanel).
Le but est de simplifier le déploiement, la gestion et l'utilisation de Caddy avec une interface graphique dédiée.

## Phase 1: Conteneurisation et Intégration Caddy/Flask (Réalisée)

Cette phase se concentre sur la mise en place de l'environnement conteneurisé et l'interaction de base entre l'UI Flask et Caddy.

### 1. Choix Techniques et Architecture Docker

*   **Image de base Docker :** `python:3.10-slim-bullseye` (pour l'application Flask et les dépendances système).
*   **Installation de Caddy2 :** Téléchargement du binaire officiel Caddy (version spécifiée par `CADDY_VERSION` dans le `Dockerfile`) depuis GitHub releases. Gestion de l'architecture (amd64, arm64) via `dpkg --print-architecture`.
*   **Gestion des Processus :** `supervisord` est utilisé pour gérer les processus Caddy et Flask à l'intérieur du conteneur.
    *   `supervisord` s'exécute en `root` pour pouvoir gérer des processus nécessitant des privilèges (comme Caddy pour les ports < 1024).
    *   Caddy est lancé par `supervisord` en tant que `root` pour se lier aux ports 80/443.
    *   L'application Flask est lancée par `supervisord` en tant qu'utilisateur non privilégié `appuser`.
*   **Communication UI Flask <-> Caddy :**
    *   **Fichier Caddyfile :** L'UI Flask lit et écrit le Caddyfile situé à un chemin fixe dans le conteneur (`/etc/caddy/Caddyfile`, configurable via `ENV CADDY_CONFIG_FILE`).
    *   **Rechargement de Caddy :** L'UI Flask déclenche un rechargement via une commande système (`caddy reload --config ...`), exécutée par le backend Flask. La commande est configurable via les préférences.
*   **Persistance des Données (Volumes Docker) :**
    *   **Caddyfile :** Monté sur `/etc/caddy/` (chemin configurable via `CADDY_CONFIG_DIR`).
    *   **Données Caddy (certificats ACME, etc.) :** Monté sur `/data/caddy` (chemin configurable via `CADDY_DATA_DIR`).
    *   **Données de l'UI (preferences.json, users.json) :** Monté sur `/app_data` (chemin configurable via `APP_DATA_DIR`).
*   **Initialisation :** Un script `entrypoint.sh` (exécuté en `root`) initialise les fichiers de configuration (`Caddyfile`, `preferences.json`, `users.json`) dans les volumes au premier démarrage s'ils sont absents ou vides. Il corrige également les chemins dans `preferences.json` pour correspondre à l'environnement du conteneur et s'assure des bonnes permissions pour `appuser` sur les volumes.
*   **Ports Exposés :** `80` (HTTP), `443` (HTTPS), et `5000` (Flask UI, configurable via `FLASK_PORT`). Le port de l'API admin de Caddy (`2019`) n'est pas exposé par défaut.
*   **Utilisateur Non-Root :** Un utilisateur `appuser` (UID/GID 1000) est créé et utilisé pour exécuter l'application Flask. Les permissions des fichiers/dossiers sont gérées en conséquence.

### 2. Modifications de l'Application Flask (`app.py`)

*   **Chemins des Fichiers :** Les constantes pour `PREFERENCES_FILE`, `USERS_FILE` et la gestion du `caddyfilePath` utilisent désormais des variables d'environnement (`APP_DATA_DIR`, `CADDY_CONFIG_FILE`) pour pointer vers les emplacements dans les volumes Docker.
*   **Préférences :**
    *   `caddyfilePath` dans les préférences est désormais informatif et forcé au chemin fixe du conteneur lors du chargement/sauvegarde.
    *   `caddyReloadCmd` est initialisé avec une commande fonctionnelle pour l'environnement conteneurisé.
*   **API Modifiées/Ajoutées :**
    *   `/` (index) : Charge le Caddyfile depuis le chemin fixe `CADDY_CONFIG_FILE`.
    *   `/api/preferences` (POST) : Ignore la valeur `caddyfilePath` envoyée par le client et force le chemin interne.
    *   `/api/caddyfile/save` (POST) : Nouvelle route pour écrire le contenu du Caddyfile fourni par le client au chemin `CADDY_CONFIG_FILE`.
    *   `/api/caddy/reload` (POST) : Nouvelle route pour exécuter la commande de rechargement de Caddy (définie dans les préférences).
*   **Initialisation (Développement Local) :** Le bloc `if __name__ == '__main__':` a été adapté pour faciliter le développement local hors Docker, mais l'initialisation principale des fichiers est déléguée à `entrypoint.sh` dans Docker.

### 3. Modifications du Frontend (`static/script.js`)

*   **Interaction avec les Nouvelles API :**
    *   Un bouton "Save Caddyfile & Reload Caddy" a été ajouté. Il appelle séquentiellement `/api/caddyfile/save` puis `/api/caddy/reload`.
    *   Des messages de statut sont affichés à l'utilisateur pour ces opérations.
*   **Préférences :**
    *   Le champ `caddyfilePath` dans l'onglet Préférences est maintenant `readonly` et affiche le chemin fixe du conteneur. Sa note explicative a été mise à jour.
    *   Le "File Browser" a une utilité réduite pour la sélection du Caddyfile principal ; son usage est clarifié.
*   **Parsing du Caddyfile :** Ajustements mineurs au regex de parsing pour une meilleure robustesse (cela reste une opération complexe et "best-effort" côté client).

### 4. Fichiers Docker

*   **`Dockerfile` :**
    *   Définit l'image, installe Python, Caddy, Supervisor.
    *   Configure les utilisateurs, les variables d'environnement, les répertoires, les volumes.
    *   Copie le code de l'application et les scripts de configuration Docker.
    *   Définit `ENTRYPOINT` et `CMD`.
*   **`CaddyPanel/docker/supervisord.conf` :** (Assumant que le dossier CaddyPanel contient l'application)
    *   Configure `supervisord` pour lancer et gérer les processus `caddy` (en `root`) et `flaskapp` (en `appuser`).
    *   Passe les variables d'environnement nécessaires à `flaskapp`.
*   **`CaddyPanel/docker/entrypoint.sh` :** (Assumant que le dossier CaddyPanel contient l'application)
    *   Script d'initialisation exécuté au démarrage du conteneur.
    *   Crée les fichiers de configuration par défaut dans les volumes si nécessaire.
    *   Met à jour les chemins dans `preferences.json`.
    *   Gère les permissions des fichiers/dossiers dans les volumes.
*   **`.dockerignore` :**
    *   Spécifie les fichiers et dossiers à exclure du contexte de build Docker pour optimiser la taille de l'image et éviter les conflits.

### 5. Instructions de Build et d'Exécution

1.  **Prérequis :** Docker (et Docker Buildx pour multi-architecture) installé.
2.  **Structure des Dossiers :**
    ```
    ProjetComplet/
    ├── CaddyPanel/  (Contient les fichiers de l'application: app.py, static/, templates/, docker/, etc.)
    │   ├── caddyfile/
    │   │   └── Caddyfile  (Exemple initial)
    │   ├── docker/
    │   │   ├── entrypoint.sh
    │   │   └── supervisord.conf
    │   ├── static/
    │   ├── templates/
    │   ├── app.py
    │   ├── preferences.json (Exemple initial)
    │   ├── requirements.txt
    │   └── users.json (Exemple initial)
    ├── Dockerfile
    └── .dockerignore
    ```
3.  **Construire l'image** (depuis `ProjetComplet/`) :
    ```bash
    docker build -t caddypanel:latest .
    ```
    Pour multi-architecture (ex: `linux/arm64` ou `linux/amd64`) :
    ```bash
    docker buildx build --platform linux/amd64 -t caddypanel:latest --load .
    ```
4.  **Préparer les répertoires hôtes pour les volumes** (une seule fois, depuis `ProjetComplet/`) :
    ```bash
    mkdir -p ./caddy_config_on_host
    mkdir -p ./caddy_data_on_host
    mkdir -p ./app_data_on_host
    ```
5.  **Exécuter le conteneur** (depuis `ProjetComplet/`) :
    Utilisez `docker-compose up -d` avec le `docker-compose.yml` mis à jour, ou la commande `docker run` suivante :
    ```bash
    docker run -d \
        -p 80:80 \
        -p 443:443 \
        -p 5000:5000 \
        -v $(pwd)/caddy_config_on_host:/etc/caddy \
        -v $(pwd)/caddy_data_on_host:/data/caddy \
        -v $(pwd)/app_data_on_host:/app_data \
        -e FLASK_SECRET_KEY="votre_cle_secrete_tres_forte_ici_!!!changez_moi!!!" \
        -e TZ="Europe/Paris" \
        --name caddypanel-instance \
        caddypanel:latest
    ```
    **Note :** Remplacer `"votre_cle_secrete_tres_forte_ici_!!!changez_moi!!!"` par une clé secrète réelle et robuste.

## Phase 2: Améliorations Futures (Planification)

### 1. Intégration de Statistiques et Monitoring

*   **Objectif :** Fournir des statistiques sur les accès, les erreurs, et d'autres informations pertinentes pour chaque site géré par Caddy.
*   **Source des Données :** Principalement les logs d'accès de Caddy (configurés en format JSON).
*   **Architecture Envisagée :**
    1.  **Collecte :** Caddy logue vers `stdout` (capturé par Docker) ou un fichier dans un volume.
    2.  **Parsing :** Un processus backend (initialement avec Flask, potentiellement avec Celery/RQ) parse les logs.
    3.  **Stockage :** Une base de données (SQLite pour commencer, puis PostgreSQL ou InfluxDB/Prometheus pour des besoins plus avancés) stockera les données parsées ou agrégées.
    4.  **API :** Le backend exposera de nouvelles API pour récupérer les statistiques.
    5.  **Visualisation :** L'UI affichera les statistiques via des tableaux et des graphiques (ex: Chart.js).
*   **Impact sur le Framework Backend :**
    *   **Flask :** Faisable, mais pourrait nécessiter l'intégration de plusieurs extensions (SQLAlchemy, Celery).
    *   **FastAPI :** Pourrait être un meilleur choix à long terme en raison de ses performances asynchrones (utiles pour le traitement des logs I/O intensif) et de sa forte orientation API avec validation de données (Pydantic).
    *   **Django :** Son ORM et son panneau d'admin pourraient être utiles, mais peut-être surdimensionné.
*   **Stratégie d'Implémentation :**
    1.  Mener à bien la Phase 1 (conteneurisation avec Flask).
    2.  Prototyper la collecte/stockage/API des statistiques de base avec Flask.
    3.  Réévaluer si les limitations de Flask justifient une migration vers FastAPI (ou autre) pour cette fonctionnalité.

### 2. Améliorations de l'Interface Utilisateur et de l'UX

*   Refonte potentielle de l'UI pour une meilleure ergonomie.
*   Amélioration du parsing/génération du Caddyfile (potentiellement déplacer une partie de la logique de parsing complexe côté backend pour plus de robustesse ou utiliser une librairie dédiée si elle existe).
*   Support pour des directives Caddy plus avancées via l'UI.
*   Gestion plus fine des utilisateurs (rôles, permissions) si nécessaire.

### 3. Sécurité et Robustesse

*   Exposition de l'API admin de Caddy de manière sécurisée si une interaction plus directe est nécessaire (actuellement, le rechargement se fait par commande).
*   Validation plus poussée des entrées utilisateur.
*   Revue de sécurité régulière.

## Décision sur le Framework Backend (Flask)

*   **Pour la Phase 1 et le début de la Phase 2 (statistiques de base) :** Rester sur **Flask** est le choix pragmatique. L'application existante est solide, et Flask est suffisant pour les fonctionnalités actuelles et les premières étapes d'intégration des statistiques.
*   **Réévaluation Future :** La pertinence de Flask sera réévaluée si les fonctionnalités de statistiques deviennent très complexes, nécessitent des performances asynchrones élevées, ou si la gestion des API devient un point central justifiant les avantages de FastAPI.

Ce plan servira de guide pour le développement et l'évolution du projet CaddyPanel.