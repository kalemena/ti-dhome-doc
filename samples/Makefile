THIS_FILE := $(lastword $(MAKEFILE_LIST))
WORKSPACE := $(PWD)
SHELL := /bin/bash

COMPOSE_FILE := docker-compose.yml
MOSQUITTO_IMAGE := eclipse-mosquitto:2.1.2-alpine
PASSWORD_FILE := workspace/mosquitto/config/password.txt
CERT_DIR := $(PWD)/workspace/mosquitto/config/certs
CERT_DAYS := 3650
CERT_CA_SUBJECT := /CN=Ti-DHome MQTT CA
CERT_SERVER_SUBJECT := /CN=mqtt
CERT_SERVER_SAN := DNS:mqtt,DNS:localhost,IP:127.0.0.1
MOSQUITTO_USER := mosquitto
COMPOSE_PROJECT_NAME := ti-dhome
NODERED_IMAGE := nodered/node-red:5.0.7
NODERED_ENV_FILE := workspace/nodered.env
NODERED_ADMIN_USER := admin
CADDY_HOSTNAME := ti-dhome.lan
VM_VERSION := v1.151.0
VMBACKUP_IMAGE := victoriametrics/vmbackup:$(VM_VERSION)
VMRESTORE_IMAGE := victoriametrics/vmrestore:$(VM_VERSION)
VM_DATA_VOLUME := $(COMPOSE_PROJECT_NAME)_victoria-metrics-data
NODERED_DATA_VOLUME := $(COMPOSE_PROJECT_NAME)_node-red-data
BACKUP_DIR := backup
VM_BACKUP_DIR := $(BACKUP_DIR)/vmbackup
HOST_UID := $(shell id -u)
HOST_GID := $(shell id -g)

.PHONY: help setup certs mosquitto-dirs password env-secret password.nodered pull up down start stop restart status logs ps backup backup.mosquitto backup.vm backup.nodered restore restore.mosquitto restore.vm restore.nodered config prune rm-volumes clean

DOCKER_COMPOSE := docker-compose -f $(COMPOSE_FILE)
STOP_SVC := $(DOCKER_COMPOSE) stop
START_SVC := $(DOCKER_COMPOSE) start

###############################
# SETUP
###############################

## help: show this help
help:
	@grep -E '^## ' $(THIS_FILE) | sed 's/^## //' | column -t -s ':'

## setup: full setup (folders, secrets, passwords, TLS certificates, pull images)
setup: mosquitto-dirs env-secret pull password password.nodered certs

## mosquitto-dirs: create the Mosquitto data/log/config folders
mosquitto-dirs:
	@mkdir -p workspace/mosquitto/data workspace/mosquitto/log workspace/mosquitto/config
	@echo "Created Mosquitto folders:"
	@echo "  - workspace/mosquitto/data"
	@echo "  - workspace/mosquitto/log"
	@echo "  - workspace/mosquitto/config"

## password: create the Mosquitto password file (asks for a password)
password: mosquitto-dirs
	@echo "Creating Mosquitto password file for user '$(MOSQUITTO_USER)' ..."
	docker run --rm -it \
		-v $(WORKSPACE)/workspace/mosquitto/data:/mosquitto/data \
		-v $(WORKSPACE)/workspace/mosquitto/log:/mosquitto/log \
		-v $(WORKSPACE)/workspace/mosquitto/config:/mosquitto/config \
		$(MOSQUITTO_IMAGE) \
		mosquitto_passwd -c /mosquitto/config/password.txt $(MOSQUITTO_USER)

## env-secret: create workspace/nodered.env with a Node-RED credential secret and Caddy defaults (idempotent)
##   consumed by compose via env_file. Generated, git-ignored workspace state.
env-secret:
	@mkdir -p workspace
	@if [ ! -f $(NODERED_ENV_FILE) ]; then \
		echo "# Runtime secrets for Node-RED and Caddy (generated, do not commit)" > $(NODERED_ENV_FILE); \
		echo "NODE_RED_CREDENTIAL_SECRET=$$(openssl rand -hex 32)" >> $(NODERED_ENV_FILE); \
		echo "NODE_RED_ADMIN_HASH=''" >> $(NODERED_ENV_FILE); \
		echo "CADDY_HOSTNAME='$(CADDY_HOSTNAME)'" >> $(NODERED_ENV_FILE); \
		echo "Created $(NODERED_ENV_FILE)"; \
		echo "Next: make password.nodered"; \
	else \
		echo "$(NODERED_ENV_FILE) already exists (delete it to regenerate)."; \
	fi

## password.nodered: set the Node-RED admin password (bcrypt hash stored in workspace/nodered.env)
password.nodered: env-secret
	@read -s -p "Node-RED admin password: " PASS1; echo; \
	 read -s -p "Confirm password: " PASS2; echo; \
	 if test "$$PASS1" != "$$PASS2" || test -z "$$PASS1"; then \
	   echo "Aborted: passwords do not match or are empty."; exit 1; \
	 fi; \
	 HASH=$$(echo "$$PASS1" | docker run --rm -i --entrypoint node $(NODERED_IMAGE) -e "const b=require('bcryptjs');let s='';process.stdin.on('data',c=>s+=c).on('end',()=>process.stdout.write(b.hashSync(s.trim(),10)))"); \
	 if test -z "$$HASH"; then \
	   echo "Failed to generate the password hash (run 'make pull' first)."; exit 1; \
	 fi; \
	 TMP=$$(mktemp); \
	 awk -v k="NODE_RED_ADMIN_HASH" -v v="'$$HASH'" 'BEGIN{done=0} $$0 ~ "^"k"=" {print k"="v; done=1; next} {print} END{if(!done) print k"="v}' $(NODERED_ENV_FILE) > "$$TMP" && mv "$$TMP" $(NODERED_ENV_FILE); \
	 echo "Node-RED admin password updated (user: $(NODERED_ADMIN_USER))."

## certs: generate the self-signed CA and server certificate for the MQTT TLS listener (idempotent, workspace is git-ignored)
certs: mosquitto-dirs
	@mkdir -p $(CERT_DIR)
	@if [ -f $(CERT_DIR)/server.crt ]; then \
		echo "Certificates already exist in $(CERT_DIR) (delete them to regenerate)."; \
	else \
		echo "Generating self-signed CA and server certificate in $(CERT_DIR) ..."; \
		openssl genrsa -out $(CERT_DIR)/ca.key 3072 2>/dev/null && \
		openssl req -x509 -new -key $(CERT_DIR)/ca.key -days $(CERT_DAYS) -subj "$(CERT_CA_SUBJECT)" -out $(CERT_DIR)/ca.crt && \
		openssl genrsa -out $(CERT_DIR)/server.key 2048 2>/dev/null && \
		openssl req -new -key $(CERT_DIR)/server.key -subj "$(CERT_SERVER_SUBJECT)" -addext "subjectAltName=$(CERT_SERVER_SAN)" -out $(CERT_DIR)/server.csr && \
		openssl x509 -req -in $(CERT_DIR)/server.csr -CA $(CERT_DIR)/ca.crt -CAkey $(CERT_DIR)/ca.key -CAcreateserial -days $(CERT_DAYS) -copy_extensions copy -out $(CERT_DIR)/server.crt && \
		rm -f $(CERT_DIR)/server.csr $(CERT_DIR)/ca.srl && \
		chmod 644 $(CERT_DIR)/ca.crt && \
		echo "Done: ca.crt, server.crt and server.key in $(CERT_DIR)"; \
	fi

## pull: pull the stack images
pull:
	docker-compose -f $(COMPOSE_FILE) pull

###############################
# STACK MANAGEMENT
###############################

## up: create and start the stack in the background
up: env-secret certs
	@grep -Eq "^NODE_RED_ADMIN_HASH='[^']" $(NODERED_ENV_FILE) || echo "WARNING: Node-RED admin password not set yet (run: make password.nodered)"
	docker-compose -f $(COMPOSE_FILE) up -d

## down: stop and remove the stack containers
down:
	docker-compose -f $(COMPOSE_FILE) down

## start: start the stack containers
start:
	docker-compose -f $(COMPOSE_FILE) start

## stop: stop the stack containers
stop:
	docker-compose -f $(COMPOSE_FILE) stop

## restart: restart the stack containers
restart:
	docker-compose -f $(COMPOSE_FILE) restart

## status: show the stack status
status:
	docker-compose -f $(COMPOSE_FILE) ps

## logs: follow the stack logs
logs:
	docker-compose -f $(COMPOSE_FILE) logs --follow

## ps: alias of status
ps: status

###############################
# BACKUP
###############################

# require-backup-file <pattern>: fail unless FILE points to an existing backup tarball
define require-backup-file
  @test -n "$(FILE)" || (echo "Usage: make $@ FILE=$(1)" && exit 1)
  @test -f "$(FILE)" || (echo "File not found: $(FILE)" && exit 1)
endef

# confirm-wipe <what>: ask for an explicit 'YES' before a destructive restore
define confirm-wipe
  @echo "WARNING: this erases the current data ($(1))."
  @read -p "Type 'YES' to proceed: " CONFIRM && test "$$CONFIRM" = "YES" || (echo "Aborted." && exit 1)
endef

# reown-host <host-dir>: give the backup files back to the host user
define reown-host
  @docker run --rm -v $(1):/backup alpine:3.20 chown -R $(HOST_UID):$(HOST_GID) /backup
endef

# backup-volume <service> <volume> <mount> <prefix>: stop the service, tar its
# named volume into backup/<prefix>-backup-<timestamp>.tgz, then restart it.
define backup-volume
  @mkdir -p $(BACKUP_DIR)
  @$(STOP_SVC) $(1)
  @docker run --rm -v $(2):$(3):ro -v $(WORKSPACE)/$(BACKUP_DIR):/backup alpine:3.20 tar -czf /backup/$(4)-backup-$$(date +%Y%m%d-%H%M%S).tgz -C $(3) .
  @$(call reown-host,$(WORKSPACE)/$(BACKUP_DIR))
  @$(START_SVC) $(1)
endef

# restore-volume <service> <volume> <mount> <pattern>: wipe a named volume and
# refill it from FILE, stopping/restarting the service around it.
define restore-volume
  @$(call require-backup-file,$(4))
  @$(call confirm-wipe,$(2))
  @$(STOP_SVC) $(1)
  @docker run --rm -v $(2):$(3) alpine:3.20 sh -c 'rm -rf $(3)/*'
  @docker run --rm -v $(2):$(3) -v $(WORKSPACE)/$(FILE):/backup/backup.tgz:ro alpine:3.20 sh -c 'tar -xzf /backup/backup.tgz -C $(3)'
  @$(START_SVC) $(1)
endef

## backup: backup all services (Mosquitto, VictoriaMetrics, Node-RED)
backup: backup.mosquitto backup.vm backup.nodered

## backup.mosquitto: create a tarball of the Mosquitto data/log/config into backup/
backup.mosquitto:
	@mkdir -p $(BACKUP_DIR)
	@tar -czf $(BACKUP_DIR)/mosquitto-backup-$$(date +%Y%m%d-%H%M%S).tgz workspace/mosquitto/data workspace/mosquitto/log workspace/mosquitto/config
	@echo "Backup created in $(BACKUP_DIR)/"

## backup.vm: incremental snapshot backup of VictoriaMetrics into backup/vmbackup
backup.vm:
	@mkdir -p $(WORKSPACE)/$(VM_BACKUP_DIR)
	@$(DOCKER_COMPOSE) --profile backup run --rm vmbackup
	@$(call reown-host,$(WORKSPACE)/$(VM_BACKUP_DIR))
	@echo "VictoriaMetrics backup done in $(VM_BACKUP_DIR)/ (owned by $(HOST_UID):$(HOST_GID))"

## backup.nodered: create a tarball of the Node-RED data volume into backup/
backup.nodered:
	@$(call backup-volume,nodered,$(NODERED_DATA_VOLUME),/data,nodered)
	@echo "Node-RED backup created in $(BACKUP_DIR)/"

## restore: list the available per-service restore commands
restore:
	@echo "Choose the service to restore:"
	@echo "  make restore.mosquitto FILE=$(BACKUP_DIR)/mosquitto-backup-<timestamp>.tgz"
	@echo "  make restore.vm"
	@echo "  make restore.nodered FILE=$(BACKUP_DIR)/nodered-backup-<timestamp>.tgz"

## restore.mosquitto: restore Mosquitto data/log/config from a tarball
restore.mosquitto:
	@$(call require-backup-file,$(BACKUP_DIR)/mosquitto-backup-<timestamp>.tgz)
	@$(STOP_SVC) mqtt
	@tar -xzf $(FILE) -C $(WORKSPACE) workspace/mosquitto/data workspace/mosquitto/log workspace/mosquitto/config
	@echo "Restore done."
	@echo "Start the stack with: make up"

## restore.vm: restore VictoriaMetrics data from backup/vmbackup (stops VM, wipes the volume)
restore.vm:
	@test -d $(WORKSPACE)/$(VM_BACKUP_DIR) || (echo "No backup found in $(VM_BACKUP_DIR)/" && exit 1)
	@$(call confirm-wipe,$(VM_DATA_VOLUME))
	@$(STOP_SVC) victoriametrics
	@docker run --rm -v $(VM_DATA_VOLUME):/victoria-metrics-data alpine:3.20 sh -c 'rm -rf /victoria-metrics-data/*'
	@docker run --rm \
		-v $(VM_DATA_VOLUME):/victoria-metrics-data \
		-v $(WORKSPACE)/$(VM_BACKUP_DIR):/backup \
		$(VMRESTORE_IMAGE) \
		-src=fs:///backup \
		-storageDataPath=/victoria-metrics-data
	@$(START_SVC) victoriametrics
	@echo "VictoriaMetrics restore done."

## restore.nodered: restore the Node-RED data volume from a tarball (stops Node-RED, wipes its volume)
restore.nodered:
	@$(call restore-volume,nodered,$(NODERED_DATA_VOLUME),/data,$(BACKUP_DIR)/nodered-backup-<timestamp>.tgz)
	@echo "Node-RED restore done."

###############################
# VALIDATION & CLEANUP
###############################

## config: validate the docker-compose file (needs workspace/nodered.env)
config: env-secret
	docker-compose -f $(COMPOSE_FILE) config

## prune: stop and remove containers, networks and orphan containers (data kept)
prune:
	docker-compose -f $(COMPOSE_FILE) down --remove-orphans

## rm-volumes: remove the named volumes (destructive, erases all data)
rm-volumes:
	docker-compose -f $(COMPOSE_FILE) down --remove-orphans --volumes

## clean: stop the stack and remove containers, networks and volumes
clean: rm-volumes