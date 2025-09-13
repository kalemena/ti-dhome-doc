THIS_FILE := $(lastword $(MAKEFILE_LIST))
SCRIPT_DIR := scripts

VM_URL ?= http://localhost:8428
VENV_DIR := $(SCRIPT_DIR)/.venv
PYTHON_BIN := $(VENV_DIR)/bin/python

.PHONY: all venv setup data-quality clean

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
	$(PYTHON_BIN) $(SCRIPT_DIR)/01-data-quality.py -u $(VM_URL) \
		--config $(SCRIPT_DIR)/energy-config.yaml

clean:
	rm -rf $(VENV_DIR) $(SCRIPT_DIR)/output