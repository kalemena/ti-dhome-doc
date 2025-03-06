
# TEMPO_SEASON := --season 2024-2025

.PHONY: venv setup

all: setup 

venv:
	python3 -m venv venv

setup: venv
	./venv/bin/pip install -r requirements.txt

tempo-costs:
	./venv/bin/python tempo-costs.py

tempo-days:
	./venv/bin/python tempo-days.py ${TEMPO_SEASON} --csv

