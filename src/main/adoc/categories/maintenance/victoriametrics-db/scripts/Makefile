THIS_FILE := $(lastword $(MAKEFILE_LIST))
WORKSPACE := $(PWD)
SHELL := /bin/bash

VM_URL := http://victoriametrics:8428
VENV_DIR := .venv-metrics
PYTHON_BIN := $(VENV_DIR)/bin/python

.PHONY: all vm.report vm.metrics-list vm.metrics-render vm.metrics-open venv.setup venv.run-metrics venv.clean

all: vm.metrics

###########################
# Victoriametrics review
###########################

vm.metrics:
	$(PYTHON_BIN) ./01-metrics-list.py -u $(VM_URL) --render

vm.metrics-open:
	open metrics-report.html

###########################
# Python virtual environment
###########################

venv.setup:
	@echo "Setting up Python virtual environment in $(VENV_DIR)..."
	python3 -m venv $(VENV_DIR)
	@echo "Installing required packages..."
	$(PYTHON_BIN) -m pip install --upgrade pip requests jinja2
	@echo "Virtual environment ready at $(VENV_DIR)"

venv.clean:
	@echo "Removing virtual environment..."
	rm -rf $(VENV_DIR)