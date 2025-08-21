# Ti-DHome - Core services sample

Docker Compose stack for the three core services of the home automation project.

## Services

| Service | Image | Version |
|---|---|---|
| MQTT broker | `eclipse-mosquitto` | 2.1.2 |
| Node-RED | `nodered/node-red` | 5.0.6 |
| Victoria Metrics | `victoriametrics/victoria-metrics` | v1.151.0 |

## Prerequisites

Docker (images use `linux/arm64`, compatible with the Odroid M1S).

## Usage

```bash
# Create the Mosquitto password file
$ docker run --rm -it -v $PWD/mosquitto/data:/mosquitto/data -v $PWD/mosquitto/log:/mosquitto/log -v $PWD/mosquitto/config:/mosquitto/config eclipse-mosquitto:2.1.2-alpine mosquitto_passwd -c /mosquitto/config/password.txt <user>

# Start the stack
$ docker-compose up -d
```

Services are exposed on:

* MQTT: `localhost:1883`
* MQTT over WebSockets: `localhost:9001`
* Node-RED: `http://localhost:1880`
* Victoria Metrics: `http://localhost:8428`

Container names follow the project convention `ti-dhome_<service>_1`
and are attached to the `ti-dhome` network so that services can reach
each other by service name (e.g. `mqtt`, `nodered`, `victoriametrics`).

## Mosquitto configuration

Edit `mosquitto/config/mosquitto.conf` to change ports, TLS or ACLs.
The configuration file is mounted read-only.