# CaddyPanel Transformation and Evolution Plan

This document describes the plan to transform the CaddyPanel project into a containerized application integrating Caddy2 and its Flask configuration interface, as well as planned future developments.

## Main Objective of the Initial Transformation

Transform the existing project into a **single Docker container** that includes:
1.  The **Caddy 2** web server.
2.  The **Flask web configuration interface** (CaddyPanel).
The goal is to simplify the deployment, management, and use of Caddy with a dedicated graphical interface.

## Phase 1: Containerization and Caddy/Flask Integration (Completed)

This phase focuses on setting up the containerized environment and the basic interaction between the Flask UI and Caddy.

### 1. Technical Choices and Docker Architecture

*   **Docker base image:** `python:3.10-slim-bullseye` (for the Flask application and system dependencies).
*   **Caddy2 Installation:** Download the official Caddy binary (version specified by `CADDY_VERSION` in the `Dockerfile`) from GitHub releases. Architecture management (amd64, arm64) via `dpkg --print-architecture`.
*   **Process Management:** `supervisord` is used to manage the Caddy and Flask processes inside the container.
    *   `supervisord` runs as `root` to be able to manage processes requiring privileges (like Caddy for ports < 1024).
    *   Caddy is launched by `supervisord` as `root` to bind to ports 80/443.
    *   The Flask application is launched by `supervisord` as a non-privileged user `appuser`.
*   **Flask UI <-> Caddy Communication:**
    *   **Caddyfile file:** The Flask UI reads and writes the Caddyfile located at a fixed path in the container (`/etc/caddy/Caddyfile`, configurable via `ENV CADDY_CONFIG_FILE`).
    *   **Caddy Reload:** The Flask UI triggers a reload via a system command (`caddy reload --config ...`), executed by the Flask backend. The command is configurable via preferences.
*   **Data Persistence (Docker Volumes):**
    *   **Caddyfile:** Mounted on `/etc/caddy/` (path configurable via `CADDY_CONFIG_DIR`).
    *   **Caddy Data (ACME certificates, etc.):** Mounted on `/data/caddy` (path configurable via `CADDY_DATA_DIR`).
    *   **UI Data (preferences.json, users.json):** Mounted on `/app_data` (path configurable via `APP_DATA_DIR`).
*   **Initialization:** An `entrypoint.sh` script (executed as `root`) initializes the configuration files (`Caddyfile`, `preferences.json`, `users.json`) in the volumes on the first start if they are absent or empty. It also corrects the paths in `preferences.json` to match the container environment and ensures the correct permissions for `appuser` on the volumes.
*   **Exposed Ports:** `80` (HTTP), `443` (HTTPS), and `5000` (Flask UI, configurable via `FLASK_PORT`). The Caddy admin API port (`2019`) is not exposed by default.
*   **Non-Root User:** A `appuser` user (UID/GID 1000) is created and used to run the Flask application. File/folder permissions are managed accordingly.

### 2. Flask Application Modifications (`app.py`)

*   **File Paths:** The constants for `PREFERENCES_FILE`, `USERS_FILE` and the management of `caddyfilePath` now use environment variables (`APP_DATA_DIR`, `CADDY_CONFIG_FILE`) to point to the locations in the Docker volumes.
*   **Preferences:**
    *   `caddyfilePath` in the preferences is now informational and forced to the fixed path of the container when loading/saving.
    *   `caddyReloadCmd` is initialized with a functional command for the containerized environment.
*   **Modified/Added APIs:**
    *   `/` (index): Loads the Caddyfile from the fixed path `CADDY_CONFIG_FILE`.
    *   `/api/preferences` (POST): Ignores the `caddyfilePath` value sent by the client and forces the internal path.
    *   `/api/caddyfile/save` (POST): New route to write the content of the Caddyfile provided by the client to the path `CADDY_CONFIG_FILE`.
    *   `/api/caddy/reload` (POST): New route to execute the Caddy reload command (defined in the preferences).
*   **Initialization (Local Development):** The `if __name__ == '__main__':` block has been adapted to facilitate local development outside of Docker, but the main initialization of the files is delegated to `entrypoint.sh` in Docker.

### 3. Frontend Modifications (`static/script.js`)

*   **Interaction with New APIs:**
    *   A "Save Caddyfile & Reload Caddy" button has been added. It sequentially calls `/api/caddyfile/save` then `/api/caddy/reload`.
    *   Status messages are displayed to the user for these operations.
*   **Preferences:**
    *   The `caddyfilePath` field in the Preferences tab is now `readonly` and displays the fixed path of the container. Its explanatory note has been updated.
    *   The "File Browser" has a reduced utility for selecting the main Caddyfile; its use is clarified.
*   **Caddyfile Parsing:** Minor adjustments to the parsing regex for better robustness (this remains a complex and "best-effort" operation on the client side).

### 4. Docker Files

*   **`Dockerfile`:**
    *   Defines the image, installs Python, Caddy, Supervisor.
    *   Configures users, environment variables, directories, volumes.
    *   Copies the application code and Docker configuration scripts.
    *   Defines `ENTRYPOINT` and `CMD`.
*   **`CaddyPanel/docker/supervisord.conf`:** (Assuming the CaddyPanel folder contains the application)
    *   Configures `supervisord` to launch and manage the `caddy` (as `root`) and `flaskapp` (as `appuser`) processes.
    *   Passes the necessary environment variables to `flaskapp`.
*   **`CaddyPanel/docker/entrypoint.sh`:** (Assuming the CaddyPanel folder contains the application)
    *   Initialization script executed at container startup.
    *   Creates the default configuration files in the volumes if necessary.
    *   Updates the paths in `preferences.json`.
    *   Manages the permissions of files/folders in the volumes.
*   **`.dockerignore`:**
    *   Specifies the files and folders to exclude from the Docker build context to optimize the image size and avoid conflicts.

### 5. Build and Execution Instructions

1.  **Prerequisites:** Docker (and Docker Buildx for multi-architecture) installed.
2.  **Folder Structure:**
    ```
    CompleteProject/
    ├── CaddyPanel/  (Contains the application files: app.py, static/, templates/, docker/, etc.)
    │   ├── caddyfile/
    │   │   └── Caddyfile  (Initial example)
    │   ├── docker/
    │   │   ├── entrypoint.sh
    │   │   └── supervisord.conf
    │   ├── static/
    │   ├── templates/
    │   ├── app.py
    │   ├── preferences.json (Initial example)
    │   ├── requirements.txt
    │   └── users.json (Initial example)
    ├── Dockerfile
    └── .dockerignore
    ```
3.  **Build the image** (from `CompleteProject/`):
    ```bash
    docker build -t caddypanel:latest .
    ```
    For multi-architecture (e.g., `linux/arm64` or `linux/amd64`):
    ```bash
    docker buildx build --platform linux/amd64 -t caddypanel:latest --load .
    ```
4.  **Prepare the host directories for the volumes** (once, from `CompleteProject/`):
    ```bash
    mkdir -p ./caddy_config_on_host
    mkdir -p ./caddy_data_on_host
    mkdir -p ./app_data_on_host
    ```
5.  **Run the container** (from `CompleteProject/`):
    Use `docker-compose up -d` with the updated `docker-compose.yml`, or the following `docker run` command:
    ```bash
    docker run -d \
        -p 80:80 \
        -p 443:443 \
        -p 5000:5000 \
        -v $(pwd)/caddy_config_on_host:/etc/caddy \
        -v $(pwd)/caddy_data_on_host:/data/caddy \
        -v $(pwd)/app_data_on_host:/app_data \
        -e FLASK_SECRET_KEY="your_very_strong_secret_key_here_!!!change_me!!!" \
        -e TZ="Europe/Paris" \
        --name caddypanel-instance \
        caddypanel:latest
    ```
    **Note:** Replace "your_very_strong_secret_key_here_!!!change_me!!!" with a real and robust secret key.

## Phase 2: Future Improvements (Planning)

### 1. Integration of Statistics and Monitoring

*   **Objective:** Provide statistics on access, errors, and other relevant information for each site managed by Caddy.
*   **Data Source:** Mainly Caddy's access logs (configured in JSON format).
*   **Envisioned Architecture:**
    1.  **Collection:** Caddy logs to `stdout` (captured by Docker) or a file in a volume.
    2.  **Parsing:** A backend process (initially with Flask, potentially with Celery/RQ) parses the logs.
    3.  **Storage:** A database (SQLite to start, then PostgreSQL or InfluxDB/Prometheus for more advanced needs) will store the parsed or aggregated data.
    4.  **API:** The backend will expose new APIs to retrieve the statistics.
    5.  **Visualization:** The UI will display the statistics via tables and graphs (e.g., Chart.js).
*   **Impact on the Backend Framework:**
    *   **Flask:** Feasible, but may require the integration of several extensions (SQLAlchemy, Celery).
    *   **FastAPI:** Could be a better long-term choice due to its asynchronous performance (useful for I/O-intensive log processing) and its strong API orientation with data validation (Pydantic).
    *   **Django:** Its ORM and admin panel could be useful, but perhaps oversized.
*   **Implementation Strategy:**
    1.  Successfully complete Phase 1 (containerization with Flask).
    2.  Prototype the collection/storage/API of basic statistics with Flask.
    3.  Re-evaluate if the limitations of Flask justify a migration to FastAPI (or other) for this functionality.

### 2. User Interface and UX Improvements

*   Potential redesign of the UI for better usability.
*   Improvement of Caddyfile parsing/generation (potentially moving some of the complex parsing logic to the backend for more robustness or using a dedicated library if one exists).
*   Support for more advanced Caddy directives via the UI.
*   Finer-grained user management (roles, permissions) if necessary.

### 3. Security and Robustness

*   Secure exposure of the Caddy admin API if more direct interaction is needed (currently, reloading is done by command).
*   More advanced validation of user input.
*   Regular security review.

## Decision on the Backend Framework (Flask)

*   **For Phase 1 and the beginning of Phase 2 (basic statistics):** Sticking with **Flask** is the pragmatic choice. The existing application is solid, and Flask is sufficient for current features and the first steps of integrating statistics.
*   **Future Re-evaluation:** The relevance of Flask will be re-evaluated if the statistics features become very complex, require high asynchronous performance, or if API management becomes a central point justifying the advantages of FastAPI.

This plan will serve as a guide for the development and evolution of the CaddyPanel project.
