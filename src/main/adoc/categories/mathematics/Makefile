THIS_FILE := $(lastword $(MAKEFILE_LIST))
SCRIPT_DIR := scripts

# Parameters - pass at the command line if you need to override:
#   make data-quality VM_URL=http://vm:8428 START=2025-09-01 END=2026-09-01
VM_URL ?= http://localhost:8428
START ?=
END ?=

VENV_DIR := $(SCRIPT_DIR)/.venv
PYTHON_BIN := $(VENV_DIR)/bin/python

ARGS := -u $(VM_URL)
ifneq ($(START),)
ARGS += --start $(START)
endif
ifneq ($(END),)
ARGS += --end $(END)
endif

.PHONY: all venv setup data-quality solar hourly-check correct-solar report-html \
	export facts contracts hourly scenarios solar-scenarios clean

all: setup data-quality

###########################
# Python virtual environment
###########################

venv:
	python3 -m venv $(VENV_DIR)

setup: venv
	$(PYTHON_BIN) -m pip install -q -r $(SCRIPT_DIR)/requirements.txt

###########################
# Data quality (Phase 0)
###########################

data-quality:
	$(PYTHON_BIN) $(SCRIPT_DIR)/01-data-quality.py $(ARGS) \
		--config $(SCRIPT_DIR)/energy-config.yaml \
		--output $(SCRIPT_DIR)/output/data-quality.json

solar:
	$(PYTHON_BIN) $(SCRIPT_DIR)/01-data-quality.py $(ARGS) \
		--config $(SCRIPT_DIR)/energy-config.yaml --checks solar \
		--output $(SCRIPT_DIR)/output/data-quality-solar.json

hourly-check:
	$(PYTHON_BIN) $(SCRIPT_DIR)/01-data-quality.py $(ARGS) \
		--config $(SCRIPT_DIR)/energy-config.yaml --checks hourly --no-coverage \
		--output $(SCRIPT_DIR)/output/data-quality-hourly.json

# Corrected solar series (backward-redistribution of opendtu catch-up
# flushes); raw VictoriaMetrics data is never modified.
correct-solar:
	$(PYTHON_BIN) $(SCRIPT_DIR)/01-data-quality.py $(ARGS) \
		--config $(SCRIPT_DIR)/energy-config.yaml \
		--correct-solar $(SCRIPT_DIR)/output/solar-corrected

report-html:
	$(PYTHON_BIN) $(SCRIPT_DIR)/01-data-quality.py $(ARGS) \
		--config $(SCRIPT_DIR)/energy-config.yaml \
		--output $(SCRIPT_DIR)/output/data-quality.json \
		--html $(SCRIPT_DIR)/output/data-quality.html

###########################
# Analysis phases (2..7)
# Targets are wired now so the Makefile matches the plan; each script lands
# with its phase. Run `make setup` once (venv + requirements) beforehand.
###########################

export:
	$(PYTHON_BIN) $(SCRIPT_DIR)/01-hourly-export.py $(ARGS) \
		--config $(SCRIPT_DIR)/energy-config.yaml \
		--output $(SCRIPT_DIR)/output/hourly-energy.csv

facts:
	$(PYTHON_BIN) $(SCRIPT_DIR)/02-yearly-facts.py \
		--config $(SCRIPT_DIR)/energy-config.yaml \
		--input $(SCRIPT_DIR)/output/hourly-energy.csv \
		--output $(SCRIPT_DIR)/output/facts.csv

contracts:
	$(PYTHON_BIN) $(SCRIPT_DIR)/03-contract-costs.py \
		--config $(SCRIPT_DIR)/energy-config.yaml \
		--input $(SCRIPT_DIR)/output/hourly-energy.csv \
		--output $(SCRIPT_DIR)/output/contracts.json

hourly:
	$(PYTHON_BIN) $(SCRIPT_DIR)/04-hourly-profile.py \
		--config $(SCRIPT_DIR)/energy-config.yaml \
		--input $(SCRIPT_DIR)/output/hourly-energy.csv \
		--output $(SCRIPT_DIR)/output/hourly-profile.csv

scenarios:
	$(PYTHON_BIN) $(SCRIPT_DIR)/05-shift-scenarios.py \
		--config $(SCRIPT_DIR)/energy-config.yaml \
		--input $(SCRIPT_DIR)/output/hourly-energy.csv \
		--output $(SCRIPT_DIR)/output/scenarios.json

solar-scenarios:
	$(PYTHON_BIN) $(SCRIPT_DIR)/06-solar-scenarios.py \
		--config $(SCRIPT_DIR)/energy-config.yaml \
		--input $(SCRIPT_DIR)/output/hourly-energy.csv \
		--output $(SCRIPT_DIR)/output/solar-scenarios.json

clean:
	rm -rf $(VENV_DIR) $(SCRIPT_DIR)/output $(SCRIPT_DIR)/cache