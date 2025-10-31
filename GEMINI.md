# GEMINI.md

## Project Overview

CaddyPanel is a simple, self-hosted web interface for managing a Caddy v2 server. Designed to run in a single Docker container, it provides an easy way to configure reverse proxies and manage your Caddyfile without manual file editing. The project is ideal for home server environments or small projects.

The application is developed in Python with the Flask framework for the backend, and uses HTML, CSS, and JavaScript for the frontend. It allows users to authenticate, view and edit the Caddyfile, reload the Caddy configuration, and view basic statistics.

## Architecture and Key Technologies

-   **Backend:** Python 3.10 with Flask.
-   **Web Server/Reverse Proxy:** Caddy v2.
-   **Containerization:** Docker and Docker Compose.
-   **In-Container Process Management:** Supervisord.
-   **Data Persistence:** Docker volumes for the Caddyfile, Caddy data (SSL certificates), and application data (users, preferences).
-   **Authentication:** Simple user system with password hashing (Werkzeug.security).
-   **Frontend:** HTML, CSS (static/style.css), JavaScript (static/script.js) for the user interface.

## Project Structure

```
CaddyPanel/
├── .dockerignore
├── app.py                  # Main Flask application
├── context.txt             # Context file (potentially for AI)
├── docker-compose.yml      # Docker Compose configuration
├── Dockerfile              # Docker image definition
├── LICENSE
├── plan.md                 # Project development plan
├── README.md               # Main project documentation
├── requirements.txt        # Python dependencies
├── .git/...                # Git directory
├── .github/                # GitHub configurations (CI/CD workflows)
│   └── workflows/
│       └── docker-publish.yml
├── caddyfile/
│   └── Caddyfile           # Default Caddy configuration file
├── docker/
│   ├── entrypoint.sh       # Docker container entry script
│   └── supervisord.conf    # Supervisord configuration
├── static/
│   ├── script.js           # Frontend JavaScript scripts
│   └── style.css           # Frontend CSS stylesheets
└── templates/
    ├── index.html          # Home page template
    ├── login.html          # Login page template
    ├── setup.html          # Initial setup page template
    └── stats.html          # Statistics page template
```

## Main Features

-   **Caddyfile Management:** Read, edit, and save the Caddyfile via the web interface.
-   **Caddy Reload:** Trigger a reload of the Caddy configuration directly from the UI.
-   **User Authentication:** Secure login system with the first user created as an administrator.
-   **Data Persistence:** Use of Docker volumes to ensure the persistence of Caddy configurations, SSL certificates, and application data.
-   **Basic Statistics:** Display of Caddy access statistics (total requests, requests per host, status codes, etc.) based on Caddy's JSON logs.

## How to Get Started (with Docker Compose)

1.  **Prerequisites:** Make sure you have Docker and Docker Compose installed on your system.
2.  **Clone the repository:**
    ```bash
    git clone <REPOSITORY_URL>
    cd CaddyPanel
    ```
3.  **Configuration:**
    *   Create a `.env` file at the root of the project (at the same level as `docker-compose.yml`).
    *   Add a robust secret key for Flask (generatable with `openssl rand -hex 32`) and your time zone:
        ```
        FLASK_SECRET_KEY=your_very_strong_secret_key_here
        TZ=Europe/Paris
        ```
4.  **Launch the application:**
    ```bash
    docker compose up -d
    ```
5.  **Access the interface:**
    Open your browser and navigate to `http://<your_server_ip_address>:5000` for initial setup and access to the panel.

## Development Conventions

-   **Backend (Flask):** The Python code follows a modular structure with `app.py` as the main entry point. Dependencies are managed via `requirements.txt`.
-   **Frontend:** Static files (CSS, JS) are in the `static/` directory, and HTML templates are in `templates/`.
-   **Docker:** The `Dockerfile` uses a multi-stage approach to optimize the final image size. `entrypoint.sh` handles container initialization and permissions, while `supervisord.conf` ensures the management of Caddy and Flask processes.
-   **Configuration:** Important file paths are defined via Docker environment variables and constants in `app.py` for better flexibility and containerization.

## Future Planning (Excerpt from `plan.md`)

The project plans for continuous improvements, including:

-   **Statistics and Monitoring:** Deeper integration of Caddy access statistics, with a potential dedicated database and new APIs for visualization.
-   **UI/UX Improvements:** Redesign of the interface for better usability and support for more advanced Caddy directives.
-   **Security and Robustness:** Increased validation of user input and regular security reviews.

The choice of Flask for the backend is maintained for the initial phases, with a re-evaluation considered if performance or API complexity needs increase significantly (for example, towards FastAPI).
