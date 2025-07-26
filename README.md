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
      # Port for the CaddyPanel web interface.
      # You can comment this out after the initial setup if you only access
      # the UI through a domain managed by Caddy.
      - "5000:5000"

    environment:
      # --- EDIT BELOW ---
      # Secret key to secure Flask sessions.
      #  !! VERY IMPORTANT !! Change this value to a long, random string.
      # You can generate one with: openssl rand -hex 32
      - FLASK_SECRET_KEY=replace-me-with-a-secure-key
      
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


## Volumes Explained

CaddyPanel uses three volumes to persist data. By using relative paths (`./`), these folders will be created in the same directory where you run `docker compose up`.

-   `./caddy_config` : Stores your `Caddyfile`. This is the main configuration file for the Caddy server.
-   `./caddy_data` : Stores Caddy's operational data, most importantly the SSL certificates it obtains from Let's Encrypt.
-   `./caddypanel_dat`` : Stores CaddyPanel's application data, such as user accounts and panel preferences.

Backing up these three folders is all you need to do to save your entire CaddyPanel setup.


## License

This project is licensed under the [MIT License](LICENSE).
