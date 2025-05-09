THIS_FILE := $(lastword $(MAKEFILE_LIST))
WORKSPACE := $(PWD)
SHELL := /bin/bash

VM_URL := http://victoriametrics:8428
VENV_DIR := .venv-metrics
PYTHON_BIN := $(VENV_DIR)/bin/python

.PHONY: all vm.report vm.metrics-list vm.metrics-render vm.metrics-open venv.setup venv.run-metrics

all: vm.report

###########################
# Victoriametrics review
###########################

vm.report: vm.metrics-list vm.metrics-render

vm.metrics-list: 
	 sh ./01-metrics-list.sh -u $(VM_URL)

vm.metrics-render:
	sh ./01-metrics-render.sh

vm.metrics-open:
	open metrics-report.html

###########################
# Python virtual environment
###########################

venv.setup:
	@echo "Setting up Python virtual environment in $(VENV_DIR)..."
	python3 -m venv $(VENV_DIR)
	@echo "Installing required packages..."
	$(PYTHON_BIN) -m pip install --upgrade pip requests
	@echo "Virtual environment ready at $(VENV_DIR)"

venv.run-metrics: venv.setup
	@echo "Running metrics list script..."
	$(PYTHON_BIN) ./01-metrics-list.py -u $(VM_URL)

venv.clean:
	@echo "Removing virtual environment..."
	rm -rf $(VENV_DIR)