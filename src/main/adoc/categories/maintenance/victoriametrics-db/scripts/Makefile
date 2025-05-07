THIS_FILE := $(lastword $(MAKEFILE_LIST))
WORKSPACE := $(PWD)
SHELL := /bin/bash

VM_URL := http://victoriametrics:8428

.PHONY: 

###########################
# Victoriametrics review
###########################

vm.metrics-list: 
	 sh ./01-metrics-list.sh -u $(VM_URL)

vm.metrics-render:
	sh ./01-metrics-render.sh

vm.metrics-open:
	open metrics-report.html