PY ?= python3

run:            ## start the playground on :9000 (CSMS) and :8000 (dashboard)
	$(PY) -m playground

test:           ## unit + end-to-end tests
	$(PY) -m unittest -v

docs:           ## regenerate the 2.0.1 vs 2.1 schema comparison
	$(PY) -m playground.tools.schema_report > docs/ocpp-2.0.1-vs-2.1.md

.PHONY: run test docs
