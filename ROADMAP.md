# CaddyPanel — Feuille de route & documentation interne

> Document de référence pour le développement et la maintenance de CaddyPanel.
> Version actuelle : v1.7

## 1. Vue d'ensemble du projet

CaddyPanel est une interface web auto-hébergée permettant de gérer un serveur **Caddy v2** (reverse proxies, Caddyfile) via une UI graphique. Conçu pour fonctionner dans un **conteneur Docker unique** embarquant Caddy, Flask et Supervisor, il est adapté à un usage de serveur personnel ou de petit projet. Le système est **single-user** : le premier utilisateur créé est l'administrateur. Le projet est **développé à 100% par intelligence artificielle** (Google Gemini) **sous supervision humaine** — cette particularité doit être prise en compte lors de l'évaluation ou modification du code.

## 2. Architecture technique

```
┌─────────────────────────────────────────────────────────┐
│                    Conteneur Docker                       │
│                                                           │
│  ┌──────────────────────────────────────────────────┐    │
│  │  supervisord  (root)                              │    │
│  │   ├─ caddy      (root, ports 80/443)             │    │
│  │  │    └─ stdout JSON → /var/log/caddy_panel/      │    │
│  │  │                  caddy_access.json.log (vol.)  │    │
│  │  │    └─ Caddyfile : /etc/caddy/Caddyfile (vol.)  │    │
│  │   └─ flaskapp   (appuser, port 5000)             │    │
│  │       └─ gunicorn → app.py (Flask)                │    │
│  │            └─ stats_aggregator.py                 │    │
│  │            └─ SQLite WAL (stats.db, vol.)         │    │
│  └──────────────────────────────────────────────────┘    │
│                                                           │
│  Volumes Docker :                                        │
│    /etc/caddy        → Caddyfile                         │
│    /data             → certificats ACME                  │
│    /app_data         → users.json, preferences.json,     │
│                        stats.db, GeoLite2-Country.mmdb    │
│    /var/log/caddy_panel → logs d'accès Caddy             │
└─────────────────────────────────────────────────────────┘
```

**Composants :**
- **`app.py`** — Application Flask : sert l'UI, expose l'API REST, gère l'authentification, les préférences, le Caddyfile, et déclenche les `caddy reload`.
- **`stats_aggregator.py`** — Processeur de logs incrémental + moteur de requêtes statistiques (SQLite WAL).
- **`static/script.js`** — Frontend vanilla JS : parsing Caddyfile, tableau de sites, modal d'édition, préférences, gestion CSRF.
- **`templates/`** — Pages HTML : `index.html` (configurateur + éditeur brut), `login.html`, `setup.html`, `stats.html`.
- **`docker/entrypoint.sh`** — Script d'initialisation : crée les fichiers par défaut, configure le logging Caddy automatiquement via heredoc Python.
- **`docker/supervisord.conf`** — Gestion des processus caddy (root) et flaskapp (appuser).
- **`Dockerfile`** — Build multi-stage, multi-arch (amd64 + arm64).

**Technologies :** Python 3.10 · Flask 3 · gunicorn · SQLite (WAL) · Caddy v2 · Supervisor · D3.js v7 + Chart.js (CDN) · Docker

## 3. Arborescence du projet

```
CaddyPanel/
├── .gitignore              # Ignore données sensibles (users.json, .env, *.mmdb, etc.)
├── .dockerignore           # Exclusions du contexte de build Docker
├── app.py                  # Application Flask principale (API + UI)
├── stats_aggregator.py     # Aggrégation stats SQLite + GeoIP optionnel
├── requirements.txt        # Dépendances : Flask, Werkzeug, gunicorn, geoip2
├── docker-compose.yml      # Orchestration Docker (service + volumes + env)
├── Dockerfile             # Image multi-stage, multi-arch
├── LICENSE
├── README.md              # Documentation utilisateur
├── GEMINI.md              # Documentation générale pour IA
├── ROADMAP.md             # Ce fichier
├── .github/
│   └── workflows/
│       └── docker-publish.yml   # CI/CD GitHub Actions
├── caddyfile/
│   └── Caddyfile          # Caddyfile d'exemple pour initialisation
├── docker/
│   ├── entrypoint.sh      # Init container, auto-config logging Caddy
│   └── supervisord.conf   # Gestion processus caddy + flaskapp
├── static/
│   ├── script.js          # Frontend JS (parsing, UI, API calls, CSRF)
│   └── style.css          # Thèmes (clair, sombre, etc.)
└── templates/
    ├── index.html         # Configurateur + éditeur Caddyfile brut + préférences
    ├── login.html         # Page de connexion
    ├── setup.html         # Création du compte admin initial
    └── stats.html         # Dashboard statistiques (KPIs, graphes, heatmap D3)
```

## 4. Routes API

| # | Méthode | Chemin | Auth | Description |
|---|---------|--------|------|-------------|
| 1 | GET | `/` | `login_required` | Page principale (configurateur + éditeur) |
| 2 | GET/POST | `/setup` | `admin_setup_required` + `csrf_required` | Création du compte admin initial |
| 3 | GET/POST | `/login` | `csrf_required` | Connexion (rate-limiting 5 tentées / 5 min) |
| 4 | GET | `/logout` | — | Déconnexion |
| 5 | GET | `/api/preferences` | `login_required` | Récupère les préférences (creds masqués) |
| 6 | POST | `/api/preferences` | `login_required` + `csrf_required` | Sauvegarde les préférences |
| 7 | POST | `/api/change-password` | `login_required` + `csrf_required` | Change le mot de passe de l'utilisateur connecté |
| 8 | POST | `/api/caddyfile/save` | `login_required` + `csrf_required` | Écrit le contenu du Caddyfile (validation: non-vide, ≤1 Mo, pas de `` ` `` ni `$(`) |
| 9 | POST | `/api/caddy/reload` | `login_required` + `csrf_required` | Recharge la config Caddy (`caddy reload`) |
| 10 | POST | `/api/caddyfile/configure_logging` | `login_required` + `csrf_required` | Auto-config du logging JSON Caddy + reload |
| 11 | GET | `/api/browse` | `login_required` | Liste les fichiers dans `FILE_BROWSE_DIR` |
| 12 | GET | `/api/readfile` | `login_required` | Lit un fichier dans `FILE_BROWSE_DIR` (blacklist `SENSITIVE_FILES`) |
| 13 | GET | `/stats` | `login_required` | Page du dashboard statistiques |
| 14 | GET | `/api/stats` | `login_required` | Données agrégées (paramètres `period`, `host`) |
| 15 | GET | `/api/stats/hosts` | `login_required` | Liste des hôtes ayant des stats |
| 16 | POST | `/api/geoip/download` | `login_required` + `csrf_required` | Télécharge la DB GeoIP via credentials MaxMind |
| 17 | POST | `/api/geoip/test` | `login_required` + `csrf_required` | Teste les credentials MaxMind sans téléchargement |
| 18 | GET | `/api/geoip/status` | `login_required` | Vérifie la disponibilité GeoIP |
| 19 | POST | `/api/geoip/upload` | `login_required` + `csrf_required` | Upload manuel d'un fichier `.mmdb` |

**Sécurité transversale :**
- **CSRF** : token généré côté serveur (`generate_csrf_token`), vérifié via `@csrf_required` sur tous les POST. Le frontend inclut `X-CSRFToken` dans chaque requête.
- **Rate-limiting** : login limité à 5 tentées par IP sur 5 minutes (en mémoire, `_login_attempts`).
- **Headers de sécurité** : `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, CSP restrictive.
- **Session** : `SESSION_PERMANENT = True`, `PERMANENT_SESSION_LIFETIME = 12h`, régénération du session ID au login (`session.clear()`).

## 5. Décisions de conception clés

### Logging Caddy — architecture critique
Caddy **n'émet pas** de logs d'accès pour un site tant que ce site n'a pas de directive `log`. Le bloc global `log { output stdout format json }` ne configure que le format de logger par défaut. Par conséquent, `entrypoint.sh` et `_configure_caddyfile_logging_internal()` ajoutent **systématiquement** :
1. Un bloc global `log { output stdout format json level INFO }`.
2. Une directive `log` dans chaque site block qui en manque.

Caddy écrit le JSON sur stdout, capturé par supervisord → `/var/log/caddy_panel/caddy_access.json.log` (persisté via volume Docker).

### Timestamps des logs
Le Caddyfile **ne doit pas** contenir `time_format rfc3339` dans le bloc `log` global, car cela produit un `ts` au format string (RFC3339) que `stats_aggregator` ne traitait pas (silencieusement ignoré). Le parseur `_parse_ts()` gère désormais les deux formats (numérique epoch et string ISO 8601), mais `time_format rfc3339` reste absent de tous les templates.

### Heredoc Python dans entrypoint.sh
L'entrypoint utilise `python3 << 'PYEOF'` (délimiteur **single-quoted**) pour éviter tout échappement bash. Les variables d'environnement sont lues via `os.environ['CADDY_CONFIG_FILE']` et non par interpolation bash.

### Cache Docker (GitHub Actions)
Le workflow GHA utilise `cache-from: type=gha` qui peut servir des layers stale. Si un changement ne semble pas pris en compte dans le conteneur, utiliser `no-cache: true` temporairement puis réactiver le cache.

### Stats SQLite incrémental
Les stats ne re-parsent pas le fichier de logs à chaque requête. `process_new_logs()` suit un offset d'octets dans la table `meta` et ne lit que les nouvelles entrées. Au premier démarrage, seul la fin du fichier est lue (limite 50 Mo / 500 000 lignes) pour éviter un traitement historique massif.

### Rétention
- `hourly_stats` : 7 jours de données détaillées par hôte.
- `daily_stats` : 365 jours de données agrégées (rollup depuis hourly toutes les 6 heures).
- Le rollup est idempotent (REPLACE) et throttlé.

## 6. Historique des versions

### v1.0 — Containerisation et UI de base
- Conteneur Docker unique (Caddy + Flask + Supervisor).
- Authentification utilisateur (Werkzeug password hashing).
- UI de gestion du Caddyfile (configurateur + éditeur brut).
- Rechargement automatique de Caddy.

### v1.1 — Parsing robuste et sécurité
- Parsing Caddyfile JS avec `findMatchingBrace()` (gestion braces imbriquées, strings, commentaires).
- Parsing Caddyfile Python avec `_find_matching_brace()` / `_remove_directive_block()`.
- XSS fix : `|replace('</', '<\\/')|safe` sur les données JSON inline.
- Hardening session : `HTTPOnly=True`, `SameSite=Lax`, clé secrète auto-générée si absente.

### v1.2 — Corrections critiques
- Fix TypeError dans les handlers d'exception (trailing commas).
- Regex Caddyfile remplacé par brace-matching parsers.
- `datetime.fromtimestamp()` corrigé pour utiliser `timezone.utc`.
- Flag `--resume` retiré de Caddy (config stale).
- `user=root` dupliqué dans supervisord.conf retiré.
- 4xx retiré du calcul d'error_rate (seulement 5xx désormais).
- `jq` remplacé par `[ -s ]` (non installé dans le conteneur).
- `CADDY_CONFIG` vs `CADDY_CONFIG_FILE` : fallback chain unifié.
- `'header'` dupliqué dans `knownDirectives` JS remplacé par `Set`.

### v1.3 — Statistiques SQLite incrémental
- Stats persistées en SQLite (perte évitée lors des restart/rotation).
- Aggrégation incrémentale via byte offset tracking.
- Rétention : 7 jours hourly + 365 jours daily.
- Filtre par hôte (bouton 📊 → `/stats?host=X`).
- Timeseries adaptatif : hourly pour 24h/7d, daily pour 30d+.

### v1.4 — Fixes critiques logging
- **entrypoint.sh** : `python3 -c "..."` remplacé par `python3 << 'PYEOF'` (fix SyntaxError échappement).
- **Caddy access logs non émis** : ajout `log` directive à chaque site block (`_add_log_to_site_blocks()`).
- **time_format rfc3339** : retiré de tous les templates Caddyfile ; `_parse_ts()` ajouté pour gérer numeric + string.
- Fix hauteur chart "Requests per Hour" (350px fixe au lieu de `min-height`).
- Fichier de logs déplacé vers `/var/log/caddy_panel/` (persisté via volume).
- `_add_log_to_site_blocks()` réécrit (docstring cassé, logique de résultat corrompue).

### v1.5 — Changement de mot de passe
- `POST /api/change-password` : validation mot de passe actuel, min 8 chars, confirmation.
- Modal de changement de mot de passe dans `index.html`.
- Logique JS dans `script.js` (validation client + appel API avec CSRF).

### v1.6-security — Hardening sécurité
- **CSRF protection** : token maison (`generate_csrf_token` / `csrf_required`) sur tous les endpoints POST/PUT/DELETE.
- **Rate-limiting login** : 5 tentées / 5 min par IP (en mémoire).
- **Restriction `/api/readfile` et `/api/browse`** : limité à `FILE_BROWSE_DIR` (dossier du Caddyfile par défaut), blacklist `SENSITIVE_FILES` (users.json, preferences.json, stats.db, .env, app.py, etc.).
- **Validation Caddyfile** : contenu non-vide, ≤1 Mo, pas de backticks ni `$(` (anti-substitution shell).
- **Session expiration** : `PERMANENT_SESSION_LIFETIME = 12h`, régénération du session ID au login.
- **Headers de sécurité** : `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, CSP restrictive.
- **Nettoyage code mort** : retrait du geo-blocking (`_apply_geoblocking_to_caddyfile`, `/api/geoip/check`, `geoBlockMode`, `geoBlockCountries`) et du health-check (`/api/sites/health-check`, `checkSitesHealth()`, `startHealthCheckLoop()`) — features incomplètes supprimées par le commit "rollback" (b661aea).
- **`.gitignore`** : créé pour exclure `users.json`, `preferences.json`, `stats.db`, `*.mmdb`, `.env`, `GeoIP.conf`, etc.
- **`requirements.txt`** : nettoyé (dépendances superflues retirées, versions pinées avec ranges).

### v1.7 — Corrections de bugs & externalisation MaxMind
- **SRI + épinglage CDN** : `integrity="sha384-..."` ajouté sur `chart.js@4.4.7`, `d3@7.9.0` et `topojson-client@3.0.0` (`stats.html`) ; `world-atlas` épinglé à `2.0.2` ; commentaire TODO retiré.
- **Credentials MaxMind externalisés** : `MAXMIND_ACCOUNT_ID` / `MAXMIND_LICENSE_KEY` (variables d'environnement) avec priorité env > `preferences.json`, fallback rétrocompatible, purge des anciennes valeurs en clair si env définies, champ `maxmindCredentialsSource` exposé dans l'API.
- **Parseur Caddyfile JS étendu** : `map`, `request_body`, `acme_server`, `basic_auth`, `handle_path`, `tracing` ajoutés à `knownDirectives` (limite restante : snippets `(nom) { ... }` non reconnus).
- **Code mort supprimé** : référence `caddyReloadCmd` retirée de `savePreferences()` (script.js) et de `docker/entrypoint.sh`.
- **Constats d'obsolescence** : les CDN étaient déjà épinglés en semver exact (le ROADMAP se trompait) ; `autoSaveAndReloadCaddy()` n'a pas de race condition (chaîne de promesses `_saveChain`, pas de booléen `isAutoSaving`).

## 7. État actuel — Bugs et limites connus

Statuts : ✅ CORRIGÉ · ⚠️ PARTIELLEMENT CORRIGÉ · ❌ OBSOLÈTE (n'était pas un bug réel) · 🔴 ACTIF (toujours valide)

### Résolus / obsolètes
- ✅ **CDN sans hash SRI** (CORRIGÉ) : `integrity="sha384-..."` ajouté sur `chart.js@4.4.7`, `d3@7.9.0` et `topojson-client@3.0.0` dans `stats.html` ; commentaire TODO retiré ; `world-atlas` épinglé à `2.0.2` (ligne 492).
- ❌ **Version CDN non épinglée** (OBSOLÈTE) : les 3 scripts étaient déjà épinglés en semver exact (`chart.js@4.4.7`, `d3@7.9.0`, `topojson-client@3.0.0`) — le ROADMAP se trompait.
- ❌ **Race condition dans `autoSaveAndReloadCaddy()`** (OBSOLÈTE) : le booléen `isAutoSaving` n'existe nulle part ; les appels sont déjà sérialisés via une chaîne de promesses `_saveChain` (`script.js:743`). Pas de race condition réelle.
- ✅ **`caddyReloadCmd` orphelin** (CORRIGÉ) : référence supprimée dans `savePreferences()` (script.js) et injection supprimée dans `docker/entrypoint.sh` ; la clé n'existait pas dans `DEFAULT_PREFERENCES` et le reload utilise une commande codée en dur.
- ✅ **Credentials MaxMind en clair** (CORRIGÉ) : externalisés vers les variables d'environnement `MAXMIND_ACCOUNT_ID` / `MAXMIND_LICENSE_KEY` (priorité env > `preferences.json`, fallback rétrocompatible, masquage GET conservé, champ `maxmindCredentialsSource` indiquant la source, purge des anciennes valeurs en clair si env définies).

### Partiellement corrigé
- ⚠️ **Parseur Caddyfile JS incomplet** (PARTIELLEMENT CORRIGÉ) : `map`, `request_body`, `acme_server`, `basic_auth`, `handle_path` et `tracing` ajoutés à `knownDirectives` (`vars`, `abort`, `handle`, `route`, `encode` étaient déjà présents). Limite restante (documentée en commentaire) : les snippets Caddyfile `(nom) { ... }` ne sont pas reconnus par le parseur et sont traités comme des site blocks.

### Toujours actifs
- 🔴 **Single-user** : pas de support multi-utilisateur ni de rôles. Un seul compte administrateur.
- 🔴 **Aucun test automatisé** : pas de tests unitaires ni d'intégration.
- 🔴 **Debug mode en dev** : `app.run(debug=True)` dans `__main__` — non utilisé en production (gunicorn), mais risqué si exécuté directement.
- 🔴 **Supervisor en root** : supervisord et caddy tournent en root (nécessaire pour ports 80/443). Compromission = accès root conteneur.

## 8. Feuille de route (roadmap)

### Court terme
- ✅ **FAIT — Épingler les versions CDN + ajouter hashes SRI** (v1.7) : `chart.js@4.4.7`, `d3@7.9.0` et `topojson-client@3.0.0` épinglés en semver exact avec `integrity="sha384-..."` dans `stats.html` ; `world-atlas` épinglé à `2.0.2`.
- ✅ **FAIT — Externaliser les credentials MaxMind** (v1.7) : `MAXMIND_ACCOUNT_ID` / `MAXMIND_LICENSE_KEY` en variables d'environnement, priorité env > `preferences.json`, fallback rétrocompatible.
- **Tests unitaires** (à faire) : parsing Caddyfile (Python + JS), validation préférences, `stats_aggregator` (aggrégation, rollup, rétention).
- **Optimisation optionnelle — Debounce `autoSaveAndReloadCaddy()`** : pas de race condition — les appels sont déjà sérialisés par une chaîne de promesses `_saveChain` (`script.js:743`) ; le booléen `isAutoSaving` n'existe pas. Un debounce/dedup resterait une optimisation pour éviter des save/reload redondants sur clics rapides.

### Moyen terme
- **Support multi-utilisateur avec rôles** : admin / éditeur / lecteur.
- **Validation Caddyfile côté serveur** : restreindre les directives autorisées, valider la structure avant `caddy reload` (potentiellement via `caddy adapt --json`).
- **Logging structuré** : remplacer les `print()` par le module `logging` de Python (niveaux, format, rotation).
- **Validation des champs site** : regex hostname pour `address`, validation `reverse_proxy` côté client et serveur.

### Long terme
- **Évaluation migration vers FastAPI** : si les statistiques deviennent complexes (async I/O, Pydantic validation).
- **Support de davantage de directives Caddy** dans l'UI configurateur (handle, route, map, encode, etc.).
- **API Caddy admin** : interagir avec l'API admin de Caddy (`localhost:2019`) au lieu de `caddy reload` via CLI.
- **Capacités Linux pour Caddy non-root** : `CAP_NET_BIND_SERVICE` pour éviter de tourner Caddy en root.

## 9. Règles de maintenance de ce fichier

- Mettre à jour ce fichier à chaque modification significative (feature, bugfix, changement d'architecture).
- Mettre à jour la section 6 (historique) à chaque nouvelle version.
- Mettre à jour la section 7 (bugs) quand un bug est corrigé ou découvert.
- Ce fichier remplace `context.txt` et `plan.md` (supprimés).