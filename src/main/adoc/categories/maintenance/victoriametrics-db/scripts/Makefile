THIS_FILE := $(lastword $(MAKEFILE_LIST))
WORKSPACE := $(PWD)
SHELL := /bin/bash

VM_URL := http://victoriametrics:8428

.PHONY: 

###########################
# Victoriametrics review
###########################

vm.metrics-list: 
	 sh ./01-list-metrics.sh $(VM_URL)
