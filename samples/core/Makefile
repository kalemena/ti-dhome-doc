THIS_FILE := $(lastword $(MAKEFILE_LIST))
WORKSPACE := $(PWD)
SHELL := /bin/bash

COMPOSE_FILE := docker-compose.yml
MOSQUITTO_IMAGE := eclipse-mosquitto:2.1.2-alpine
PASSWORD_FILE := mosquitto/config/password.txt
MOSQUITTO_USER := mosquitto
BACKUP_DIR := backup

.PHONY: help setup mosquitto-dirs password pull up down start stop restart status logs ps backup restore config prune rm-volumes clean

###############################
# SETUP
###############################

## help: show this help
help:
	@grep -E '^## ' $(THIS_FILE) | sed 's/^## //' | column -t -s ':'

## setup: setup the stack (create folders, Mosquitto password, pull images)
setup: mosquitto-dirs password pull

## mosquitto-dirs: create the Mosquitto data/log/config folders
mosquitto-dirs:
	@mkdir -p mosquitto/data mosquitto/log mosquitto/config
	@echo "Created Mosquitto folders:"
	@echo "  - mosquitto/data"
	@echo "  - mosquitto/log"
	@echo "  - mosquitto/config"

## password: create the Mosquitto password file (asks for a password)
password: mosquitto-dirs
	@echo "Creating Mosquitto password file for user '$(MOSQUITTO_USER)' ..."
	docker run --rm -it \
		-v $(WORKSPACE)/mosquitto/data:/mosquitto/data \
		-v $(WORKSPACE)/mosquitto/log:/mosquitto/log \
		-v $(WORKSPACE)/mosquitto/config:/mosquitto/config \
		$(MOSQUITTO_IMAGE) \
		mosquitto_passwd -c /mosquitto/config/password.txt $(MOSQUITTO_USER)

## pull: pull the stack images
pull:
	docker-compose -f $(COMPOSE_FILE) pull

###############################
# STACK MANAGEMENT
###############################

## up: create and start the stack in the background
up:
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

## backup: create a tarball of the Mosquitto data/log/config into backup/
backup:
	@mkdir -p $(BACKUP_DIR)
	@tar -czf $(BACKUP_DIR)/mosquitto-backup-$$(date +%Y%m%d-%H%M%S).tgz mosquitto/data mosquitto/log mosquitto/config
	@echo "Backup created in $(BACKUP_DIR)/"

## restore: restore data/log/config from a tarball (run make restore FILE=backup/xxx.tgz)
restore:
	@test -n "$(FILE)" || (echo "Usage: make restore FILE=$(BACKUP_DIR)/mosquitto-backup-<timestamp>.tgz" && exit 1)
	@test -f "$(FILE)" || (echo "File not found: $(FILE)" && exit 1)
	@docker-compose -f $(COMPOSE_FILE) stop mqtt
	@tar -xzf $(FILE) -C $(WORKSPACE) mosquitto/data mosquitto/log mosquitto/config
	@echo "Restore done."
	@echo "Start the stack with: make up"

###############################
# VALIDATION & CLEANUP
###############################

## config: validate the docker-compose file
config:
	docker-compose -f $(COMPOSE_FILE) config

## prune: stop and remove containers, networks and orphan containers (data kept)
prune:
	docker-compose -f $(COMPOSE_FILE) down --remove-orphans

## rm-volumes: remove the named volumes (destructive, erases all data)
rm-volumes:
	docker-compose -f $(COMPOSE_FILE) down --remove-orphans --volumes

## clean: stop the stack and remove containers, networks and volumes
clean: rm-volumes