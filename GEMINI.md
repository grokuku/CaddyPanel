# GEMINI.md

## Project Overview

CaddyPanel est une interface web simple et auto-hébergée pour gérer un serveur Caddy v2. Conçue pour fonctionner dans un conteneur Docker unique, elle offre un moyen facile de configurer des proxys inverses et de gérer votre Caddyfile sans édition manuelle de fichiers. Le projet est idéal pour les environnements de serveurs domestiques ou de petits projets.

L'application est développée en Python avec le framework Flask pour le backend, et utilise HTML, CSS et JavaScript pour le frontend. Elle permet aux utilisateurs de s'authentifier, de visualiser et modifier le Caddyfile, de recharger la configuration de Caddy et de consulter des statistiques de base.

## Architecture et Technologies Clés

-   **Backend:** Python 3.10 avec Flask.
-   **Serveur Web/Proxy Inverse:** Caddy v2.
-   **Conteneurisation:** Docker et Docker Compose.
-   **Gestion des Processus dans le Conteneur:** Supervisord.
-   **Persistance des Données:** Volumes Docker pour le Caddyfile, les données de Caddy (certificats SSL) et les données de l'application (utilisateurs, préférences).
-   **Authentification:** Système d'utilisateurs simple avec hachage de mots de passe (Werkzeug.security).
-   **Frontend:** HTML, CSS (static/style.css), JavaScript (static/script.js) pour l'interface utilisateur.

## Structure du Projet

```
CaddyPanel/
├── .dockerignore
├── app.py                  # Application Flask principale
├── context.txt             # Fichier de contexte (potentiellement pour l'IA)
├── docker-compose.yml      # Configuration Docker Compose
├── Dockerfile              # Définition de l'image Docker
├── LICENSE
├── plan.md                 # Plan de développement du projet
├── README.md               # Documentation principale du projet
├── requirements.txt        # Dépendances Python
├── .git/...                # Répertoire Git
├── .github/                # Configurations GitHub (workflows CI/CD)
│   └── workflows/
│       └── docker-publish.yml
├── caddyfile/
│   └── Caddyfile           # Fichier de configuration Caddy par défaut
├── docker/
│   ├── entrypoint.sh       # Script d'entrée du conteneur Docker
│   └── supervisord.conf    # Configuration de Supervisord
├── static/
│   ├── script.js           # Scripts JavaScript du frontend
│   └── style.css           # Feuilles de style CSS du frontend
└── templates/
    ├── index.html          # Modèle de page d'accueil
    ├── login.html          # Modèle de page de connexion
    ├── setup.html          # Modèle de page de configuration initiale
    └── stats.html          # Modèle de page de statistiques
```

## Fonctionnalités Principales

-   **Gestion du Caddyfile:** Lecture, édition et sauvegarde du Caddyfile via l'interface web.
-   **Rechargement de Caddy:** Déclenchement du rechargement de la configuration Caddy directement depuis l'UI.
-   **Authentification Utilisateur:** Système de connexion sécurisé avec création du premier utilisateur comme administrateur.
-   **Persistance des Données:** Utilisation de volumes Docker pour assurer la persistance des configurations Caddy, des certificats SSL et des données de l'application.
-   **Statistiques Basiques:** Affichage de statistiques d'accès Caddy (total des requêtes, requêtes par hôte, codes de statut, etc.) basées sur les logs JSON de Caddy.

## Comment Démarrer (avec Docker Compose)

1.  **Prérequis:** Assurez-vous que Docker et Docker Compose sont installés sur votre système.
2.  **Cloner le dépôt:**
    ```bash
    git clone <URL_DU_DEPOT>
    cd CaddyPanel
    ```
3.  **Configuration:**
    *   Créez un fichier `.env` à la racine du projet (au même niveau que `docker-compose.yml`).
    *   Ajoutez-y une clé secrète robuste pour Flask (générable avec `openssl rand -hex 32`) et votre fuseau horaire:
        ```
        FLASK_SECRET_KEY=votre_cle_secrete_tres_forte_ici
        TZ=Europe/Paris
        ```
4.  **Lancer l'application:**
    ```bash
    docker compose up -d
    ```
5.  **Accéder à l'interface:**
    Ouvrez votre navigateur et naviguez vers `http://<adresse_ip_de_votre_serveur>:5000` pour la configuration initiale et l'accès au panneau.

## Conventions de Développement

-   **Backend (Flask):** Le code Python suit une structure modulaire avec `app.py` comme point d'entrée principal. Les dépendances sont gérées via `requirements.txt`.
-   **Frontend:** Les fichiers statiques (CSS, JS) sont dans le répertoire `static/`, et les modèles HTML dans `templates/`.
-   **Docker:** Le `Dockerfile` utilise une approche multi-stage pour optimiser la taille de l'image finale. `entrypoint.sh` gère l'initialisation du conteneur et les permissions, tandis que `supervisord.conf` assure la gestion des processus Caddy et Flask.
-   **Configuration:** Les chemins de fichiers importants sont définis via des variables d'environnement Docker et des constantes dans `app.py` pour une meilleure flexibilité et conteneurisation.

## Planification Future (Extrait de `plan.md`)

Le projet prévoit des améliorations continues, notamment:

-   **Statistiques et Monitoring:** Intégration plus poussée de statistiques d'accès Caddy, avec potentiellement une base de données dédiée et de nouvelles API pour la visualisation.
-   **Améliorations UI/UX:** Refonte de l'interface pour une meilleure ergonomie et support de directives Caddy plus avancées.
-   **Sécurité et Robustesse:** Validation accrue des entrées utilisateur et revues de sécurité régulières.

Le choix de Flask pour le backend est maintenu pour les phases initiales, avec une réévaluation envisagée si les besoins en performance ou en complexité des API augmentent significativement (par exemple, vers FastAPI).