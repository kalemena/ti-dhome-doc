# Ti-DHome - Core services sample

Docker Compose stack for the three core services of the home automation project
(MQTT broker, Node-RED, Victoria Metrics).

## Services

| Service | Image | Version |
|---|---|---|
| MQTT broker | `eclipse-mosquitto` | 2.1.2 |
| Node-RED | `nodered/node-red` | 5.0.7 |
| Caddy | `caddy` | 2.11.4 |
| Victoria Metrics | `victoriametrics/victoria-metrics` | v1.151.0 |

## Prerequisites

* Docker with the compose plugin, or the standalone `docker-compose` command
* Images use `linux/arm64`, compatible with the Odroid M1S
* `make` (GNU Make) to use the provided Makefile

## Persistence

Mosquitto state is stored in **bind mounts**: the committed configuration under
`etc/mosquitto/` (read-only) and the runtime state under `workspace/mosquitto/`,
so the files are directly readable/backupable on the host. Node-RED, Caddy and
Victoria Metrics use Docker *named volumes*.

| Service | Storage | Location |
|---|---|---|
| Mosquitto (config) | bind mount (read-only) | `etc/mosquitto/config/` (`mosquitto.conf`) |
| Mosquitto (data) | bind mount | `workspace/mosquitto/data/` (`mosquitto.db`) |
| Mosquitto (logs) | bind mount | `workspace/mosquitto/log/` |
| Mosquitto (password) | bind mount | `workspace/mosquitto/config/` (`password.txt`) |
| Mosquitto (TLS certificates) | bind mount | `workspace/mosquitto/config/certs/` (created by `make certs`) |
| Node-RED | named volume | `ti-dhome_node-red-data` |
| Caddy (TLS state + config) | named volumes | `ti-dhome_caddy-data`, `ti-dhome_caddy-config` |
| Victoria Metrics | named volume | `ti-dhome_victoria-metrics-data` |

Bind-mount directories and backups are git-ignored (see `.gitignore`).

## Quick start

```bash
$ make setup    # create folders, secrets, Mosquitto + Node-RED passwords, pull images
$ make up       # start the stack detached
$ make status   # show containers status
```

`make setup` prompts for passwords interactively: the Mosquitto broker user and
the Node-RED admin user. Secrets live in git-ignored `workspace/` files.

## Configuration

| File | Purpose |
|---|---|
| `docker-compose.yml` | Services, ports, volumes, network |
| `etc/mosquitto/config/mosquitto.conf` | Mosquitto configuration (read-only mount) |
| `workspace/mosquitto/config/password.txt` | Mosquitto credentials, created by `make password` |
| `workspace/mosquitto/config/certs/` | TLS certificates for the `8883` listener, created by `make certs` |
| `etc/caddy/Caddyfile` | Caddy reverse proxy: TLS termination in front of Node-RED |
| `etc/nodered/settings.js` | Node-RED security settings (`adminAuth`, `httpNodeAuth`, `credentialSecret`) |
| `workspace/nodered.env` | Node-RED/Caddy runtime secrets, created by `make env-secret` |

`workspace/mosquitto/config/password.txt` is created by `make password` and is
expected to contain a user named `mosquitto` (override with
`make password MOSQUITTO_USER=foo`). The file is sensitive: it should never be
committed to version control.

`workspace/nodered.env` is created by `make env-secret` (non-interactive) and
holds `NODE_RED_CREDENTIAL_SECRET` (used to encrypt `flows_cred.json`),
`NODE_RED_ADMIN_HASH` (set by `make password.nodered`) and `CADDY_HOSTNAME`.
It is equally sensitive and git-ignored.

## Makefile commands

Run `make help` to list all commands.

### Setup

| Command | Description |
|---|---|
| `setup` | Full setup: create folders, secrets, Mosquitto + Node-RED passwords, TLS certificates, pull images |
| `mosquitto-dirs` | Create the Mosquitto `data`/`log`/`config` folders |
| `password` | Create the Mosquitto password file (interactive) |
| `env-secret` | Create `workspace/nodered.env` with a fresh Node-RED credential secret and Caddy default hostname (non-interactive, idempotent) |
| `password.nodered` | Set the Node-RED admin password (bcrypt hash into `workspace/nodered.env`) |
| `certs` | Generate a self-signed CA and server certificate for the MQTT TLS listener (idempotent) |
| `pull` | Pull the image versions pinned in `docker-compose.yml` |

### Lifecycle

| Command | Description |
|---|---|
| `up` | Create and start the stack in the background |
| `down` | Stop and remove the stack containers (data is kept) |
| `start` | Start existing stopped containers |
| `stop` | Stop the stack containers |
| `restart` | Restart the stack containers |
| `status` / `ps` | Show the stack containers status |
| `logs` | Follow the stack logs (Ctrl-C to exit) |

### Validation & cleanup

| Command | Description |
|---|---|
| `config` | Validate `docker-compose.yml` (also creates `workspace/nodered.env` if missing) |
| `prune` | `down`, plus removal of orphan containers (volumes and data kept) |
| `rm-volumes` | Data removal: `down` with removal of containers, networks and named volumes |
| `clean` | Alias of `rm-volumes` (erases all data) |

### Backup & restore

| Command | Description |
|---|---|
| `backup` | Backup all services (runs `backup.mosquitto` + `backup.vm` + `backup.nodered`) |
| `backup.mosquitto` | Tarball of the Mosquitto data/log/config into `backup/` |
| `backup.vm` | Incremental snapshot backup of Victoria Metrics into `backup/vmbackup/` |
| `backup.nodered` | Tarball of the Node-RED data volume into `backup/` |
| `restore` | List the available per-service restore commands |
| `restore.mosquitto FILE=backup/xxx.tgz` | Restore Mosquitto state from a tarball |
| `restore.vm` | Restore Victoria Metrics from `backup/vmbackup/` (stops the VM, wipes its volume) |
| `restore.nodered FILE=backup/xxx.tgz` | Restore the Node-RED data volume from a tarball (stops Node-RED, wipes its volume) |

`backup` runs the three `backup.*` targets back to back; each stops/restarts its
service around the copy. Restores are deliberately *not* aggregated: they are
destructive, so each service has its own explicit `restore.*` command — run
`make restore` to list them. Run `make backup` first, then `make restore.<svc>`.

Mosquitto is tiny, so it is backed up with a plain `tar` of its bind mounts
(safest to `make stop` first for a consistent `mosquitto.db`).

Victoria Metrics (tens of GB) uses the official `vmbackup`/`vmrestore` tools:
`backup.vm` calls the `/snapshot` API (zero downtime), then does an
**incremental**+compressed copy to `backup/vmbackup/`. `restore.vm` wipes and
refills the `victoria-metrics-data` volume from the latest backup — the stack
must be stopped, and the command asks for confirmation. For offsite copies,
point `-dst` at an S3/GCS bucket instead of `fs:///backup`.

The one-shot `vmbackup` service mounts `vmbackup-tmp` at `/tmp`: it is dedicated
scratch space (via the tool's `-tmpdir`) for staging part files during a backup,
kept off the container's ephemeral overlay layer. Safe to keep; it matters most
for large DBs and when backing up to a remote destination, where uploads are
staged heavily.

`vmbackup` runs as root (it must read the root-owned `victoria-metrics-data`
volume), so `backup.vm` re-owns `backup/vmbackup` back to your user afterwards
via a throwaway container — no host `sudo` required.

Node-RED volume (Node-RED flows, settings and installed node modules, typically
a few MB) is backed up as a plain `tar` of its named volume by a one-shot
`alpine` container: `backup.nodered` stops the container for a consistent
`flows.json`, tars the volume into `backup/nodered-backup-<timestamp>.tgz`, and
restarts it. `restore.nodered` wipes and refills the `node-red-data` volume from
the given tarball (the stack must be stopped and the command asks for
confirmation, like `restore.vm`). The tarballs are root-owned (alpine runs as
root), so the backup file is re-owned to your user the same way as `backup.vm`.

`rm-volumes` / `clean` are *destructive*: they erase metrics, flows and MQTT state.
Run `backup` (or any `backup.*` target) first.

## Services

### Endpoints

| Service | Address | Purpose |
|---|---|---|
| MQTT | `localhost:1883` | MQTT 3.1.1 / 5 clients |
| MQTT over TLS | `localhost:8883` | MQTT with TLS (`mqtts://`), see Security |
| MQTT over WebSockets | `localhost:9001` | Browser / dashboard clients |
| Caddy | `localhost:443` (HTTPS) | Reverse proxy, TLS termination |
| Node-RED | `https://ti-dhome.lan` (via Caddy) | Editor and HTTP endpoints |
| Victoria Metrics | `http://localhost:8428` | VMUI, write and query API |

Node-RED is only served through Caddy: from a browser, resolve `ti-dhome.lan`
to the stack host (`/etc/hosts` or DNS) and either accept the self-signed
certificate once or install Caddy's internal CA from the container:

```bash
$ docker exec ti-dhome-caddy-1 cat /data/caddy/pki/authorities/local/root.crt
```

### Networking

The project name `ti-dhome` is defined in `.env` via `COMPOSE_PROJECT_NAME`.
It prefixes all auto-generated resource names:

| Pattern | Example |
|---|---|
| Container | `ti-dhome-<service>-1` (`ti-dhome-mqtt-1`) |
| Volume | `ti-dhome_<volume>` (`ti-dhome_mosquitto-data`) |
| Network | `ti-dhome` (explicit `name:`) |

Each service joins the `ti-dhome` network and can reach the others by service
name (`mqtt`, `caddy`, `nodered`, `victoriametrics`). MQTT, Victoria Metrics and
Grafana ports are published to the host so external devices and dashboards can
connect. Node-RED (`1880`) is intentionally **not** published: it is only
reachable through the Caddy reverse proxy on `443`, with TLS terminated there.

### Security

* **MQTT** has anonymous access disabled and uses the `mosquitto_passwd`
  password file; the plain, WebSocket and TLS listeners all require credentials.
* **MQTT over TLS** is available on port `8883`. `make certs` (run automatically
  by `setup`/`up`) generates a self-signed CA plus a server certificate — valid
  for `mqtt` (service name) and `localhost` — in `workspace/mosquitto/config/certs/`.
  The certs are git-ignored workspace state: treat the generated keys as
  development-only material and replace them with properly managed certificates
  for anything exposed off the trusted network. Client certificates are not
  required, only the server certificate is verified.
* **TLS for the HTTP services is terminated by the Caddy reverse proxy**, not by
  Node-RED itself. Node-RED's port `1880` stays on the internal `ti-dhome`
  network and Caddy proxies `nodered:1880` through HTTPS. By default Caddy uses
  its own internal CA (`tls internal`, self-signed): no public DNS needed, ideal
  for a LAN. For internet exposure, remove that line, point a public domain at
  the host and Caddy will obtain and auto-renew Let's Encrypt certificates.
* **Node-RED itself is protected by real credentials**
  (`etc/nodered/settings.js`, mounted read-only): `adminAuth` guards the editor
  and admin API, `httpNodeAuth` guards HTTP nodes, and
  `NODE_RED_CREDENTIAL_SECRET` encrypts the flow credentials in
  `flows_cred.json`. The admin password bcrypt hash and the credential secret
  are generated into the git-ignored `workspace/nodered.env` by
  `make env-secret` + `make password.nodered` and injected as container
  environment variables. The setup fails closed: until the password is set,
  logins are rejected.
* Hardening at the proxy (rate limiting, request logging, and an optional
  second `basic_auth` layer — see the snippet in `Caddyfile`) can be layered on
  without touching the services.
* Only the ports listed above are published to the host; nothing binds all
  services to a public address by default.

## Node-RED flows

A sample routing flow subscribes to `teleinfo/#`, `zigbee/#` and `opendtu/#` and
forwards the measurements to Victoria Metrics.

`etc/nodered/flows/mosquitto-tls.json` is a first flow connecting to Mosquitto
over TLS (`mqtts://mqtt:8883`): it listens on `test/#` and publishes a timestamp
every 10s on `test/from-nodered`, so the round trip is visible in the debug
sidebar. The broker CA (`ca.crt`) is mounted read-only into the Node-RED
container at `/certs/`; before deploying, open the broker node and fill in the
Mosquitto password (user `mosquitto`) on the Security tab.

Open the editor at `https://ti-dhome.lan/` and sign in with the admin user set
by `make password.nodered`. Import any flow from the editor UI once the stack
is running (`Menu ▸ Import`, then paste the JSON, or drag the file onto the
editor).

## Upgrade images

The image versions are pinned in `docker-compose.yml`. To upgrade:

1. Bump the version tags in `docker-compose.yml`
2. `make pull`
3. `make up` (recreates the containers)

Data is preserved in the named volumes across upgrades.