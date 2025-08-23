# Ti-DHome - Core services sample

Docker Compose stack for the three core services of the home automation project
(MQTT broker, Node-RED, Victoria Metrics).

## Services

| Service | Image | Version |
|---|---|---|
| MQTT broker | `eclipse-mosquitto` | 2.1.2 |
| Node-RED | `nodered/node-red` | 5.0.6 |
| Victoria Metrics | `victoriametrics/victoria-metrics` | v1.151.0 |

## Prerequisites

* Docker with the compose plugin, or the standalone `docker-compose` command
* Images use `linux/arm64`, compatible with the Odroid M1S
* `make` (GNU Make) to use the provided Makefile

The stack relies on Docker *named volumes* to persist data:

| Volume | Service |
|---|---|
| `mosquitto-data` | MQTT broker (retained messages, sessions) |
| `mosquitto-log` | MQTT broker (logs) |
| `node-red-data` | Node-RED (flows, nodes, credentials) |
| `victoria-metrics-data` | Victoria Metrics (metrics) |

## Quick start

```bash
$ make setup    # create folders, Mosquitto password, pull images
$ make up       # start the stack detached
$ make status   # show containers status
```

## Configuration

| File | Purpose |
|---|---|
| `docker-compose.yml` | Services, ports, volumes, network |
| `mosquitto/config/mosquitto.conf` | Mosquitto configuration (read-only mount) |
| `mosquitto/config/password.txt` | Mosquitto credentials, created by `make password` |

`mosquitto/config/password.txt` is created by `make password` and is expected to
contain a user named `mosquitto` (override with `make password MOSQUITTO_USER=foo`).
The file is sensitive: it should never be committed to version control.

## Makefile commands

Run `make help` to list all commands.

### Setup

| Command | Description |
|---|---|
| `setup` | Full setup: create folders, Mosquitto password, pull images |
| `mosquitto-dirs` | Create the Mosquitto `data`/`log`/`config` folders |
| `password` | Create the Mosquitto password file (interactive) |
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
| `config` | Validate `docker-compose.yml` |
| `prune` | `down`, plus removal of orphan containers (volumes and data kept) |
| `rm-volumes` | Data removal: `down` with removal of containers, networks and named volumes |
| `clean` | Alias of `rm-volumes` (erases all data) |

`rm-volumes` / `clean` are *destructive*: they erase metrics, flows and MQTT state.
A Victoria Metrics backup should be taken first (see the *Maintenance* category).

## Services

### Endpoints

| Service | Address | Purpose |
|---|---|---|
| MQTT | `localhost:1883` | MQTT 3.1.1 / 5 clients |
| MQTT over WebSockets | `localhost:9001` | Browser / dashboard clients |
| Node-RED | `http://localhost:1880` | Editor and HTTP endpoints |
| Victoria Metrics | `http://localhost:8428` | VMUI, write and query API |

### Networking

The project name `ti-dhome` is defined in `.env` via `COMPOSE_PROJECT_NAME`.
It prefixes all auto-generated resource names:

| Pattern | Example |
|---|---|
| Container | `ti-dhome-<service>-1` (`ti-dhome-mqtt-1`) |
| Volume | `ti-dhome_<volume>` (`ti-dhome_mosquitto-data`) |
| Network | `ti-dhome` (explicit `name:`) |

Each service joins the `ti-dhome` network and can reach the others by service
name (`mqtt`, `nodered`, `victoriametrics`). Ports are published to the host so
external devices and dashboards can connect.

### Security

* **MQTT** has anonymous access disabled and uses the `mosquitto_passwd`
  password file; both the plain and WebSocket listeners require credentials.
* Only the ports listed above are published to the host; nothing binds all
  services to a public address by default.
* Node-RED and Victoria Metrics come without built-in authentication: keep them
  on a trusted network and put a reverse proxy with authentication in front of
  anything reachable beyond that.

## Node-RED flows

A sample routing flow subscribes to `teleinfo/#`, `zigbee/#` and `opendtu/#` and
forwards the measurements to Victoria Metrics. Import any flow from the Node-RED
editor UI once the stack is running.

## Upgrade images

The image versions are pinned in `docker-compose.yml`. To upgrade:

1. Bump the version tags in `docker-compose.yml`
2. `make pull`
3. `make up` (recreates the containers)

Data is preserved in the named volumes across upgrades.