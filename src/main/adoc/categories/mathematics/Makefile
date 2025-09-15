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

.PHONY: all venv setup data-quality solar report-html clean

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

report-html:
	$(PYTHON_BIN) $(SCRIPT_DIR)/01-data-quality.py $(ARGS) \
		--config $(SCRIPT_DIR)/energy-config.yaml \
		--output $(SCRIPT_DIR)/output/data-quality.json \
		--html $(SCRIPT_DIR)/output/data-quality.html

clean:
	rm -rf $(VENV_DIR) $(SCRIPT_DIR)/output