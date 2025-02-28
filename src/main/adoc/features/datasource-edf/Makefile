
TEMPO_SEASON := 2024-2025

.PHONY: venv setup

all: setup 

venv:
	python3 -m venv venv

setup: venv
	./venv/bin/pip install -r requirements.txt


tempo-days:
	./venv/bin/python tempo-days.py --season ${TEMPO_SEASON} --csv

