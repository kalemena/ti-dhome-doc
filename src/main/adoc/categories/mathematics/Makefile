THIS_FILE := $(lastword $(MAKEFILE_LIST))
SCRIPT_DIR := scripts

# Parameters - pass at the command line if you need to override:
#   make data-quality VM_URL=http://vm:8428 START=2025-09-01 END=2026-09-01
# VM_URL is used by every phase; START/END override the configured window for
# the data-quality family only (01-data-quality.py, which accepts them).
VM_URL ?= http://localhost:8428
START ?=
END ?=

VENV_DIR := $(SCRIPT_DIR)/.venv
PYTHON_BIN := $(VENV_DIR)/bin/python

VM_ARGS := -u $(VM_URL)
DX_ARGS := $(VM_ARGS)
ifneq ($(START),)
DX_ARGS += --start $(START)
endif
ifneq ($(END),)
DX_ARGS += --end $(END)
endif

.PHONY: all venv setup data-quality solar hourly-check correct-solar report-html \
	export facts contracts hourly scenarios solar-scenarios clean

# Phase 8: `make all` drives the analysis phases 2..7 in order (data-quality
# checks stay separate targets). `setup` boots the venv first.
all: setup export facts contracts hourly scenarios solar-scenarios

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
	$(PYTHON_BIN) $(SCRIPT_DIR)/01-data-quality.py $(DX_ARGS) \
		--config $(SCRIPT_DIR)/energy-config.yaml \
		--output $(SCRIPT_DIR)/output/data-quality.json

solar:
	$(PYTHON_BIN) $(SCRIPT_DIR)/01-data-quality.py $(DX_ARGS) \
		--config $(SCRIPT_DIR)/energy-config.yaml --checks solar \
		--output $(SCRIPT_DIR)/output/data-quality-solar.json

hourly-check:
	$(PYTHON_BIN) $(SCRIPT_DIR)/01-data-quality.py $(DX_ARGS) \
		--config $(SCRIPT_DIR)/energy-config.yaml --checks hourly --no-coverage \
		--output $(SCRIPT_DIR)/output/data-quality-hourly.json

# Corrected solar series (backward-redistribution of opendtu catch-up
# flushes); raw VictoriaMetrics data is never modified.
correct-solar:
	$(PYTHON_BIN) $(SCRIPT_DIR)/01-data-quality.py $(DX_ARGS) \
		--config $(SCRIPT_DIR)/energy-config.yaml \
		--correct-solar $(SCRIPT_DIR)/output/solar-corrected

report-html:
	$(PYTHON_BIN) $(SCRIPT_DIR)/01-data-quality.py $(DX_ARGS) \
		--config $(SCRIPT_DIR)/energy-config.yaml \
		--output $(SCRIPT_DIR)/output/data-quality.json \
		--html $(SCRIPT_DIR)/output/data-quality.html

###########################
# Analysis phases (2..7)
# Ran in order by `make all` (export must come first: phases 3..7 read
# scripts/output/hourly-energy.csv). Every phase after 2 works offline from
# the exported record; raw VM readings are cached under scripts/cache/.
###########################

export:
	$(PYTHON_BIN) $(SCRIPT_DIR)/01-hourly-export.py $(VM_ARGS) \
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
	rm -rf $(VENV_DIR) $(SCRIPT_DIR)/output $(SCRIPT_DIR)/cache $(SCRIPT_DIR)/__pycache__