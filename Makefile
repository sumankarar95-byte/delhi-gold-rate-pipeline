PY ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
RUN = PYTHONPATH=src $(PY) -m goldpipe.pipeline

.PHONY: setup run extract transform quality report calibrate import-ibja test clean site
setup:      ; python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
run:        ; $(RUN) run
extract:    ; $(RUN) extract
transform:  ; $(RUN) transform
quality:    ; $(RUN) quality
report:     ; $(RUN) report
calibrate:  ; $(RUN) calibrate $(RATE) && $(RUN) transform && $(RUN) report
import-ibja: ; $(RUN) import-ibja $(CSV) && $(RUN) transform && $(RUN) report
test:       ; PYTHONPATH=src $(PY) -m pytest -q tests
clean:      ; rm -rf data reports

# Static site for GitHub Pages: latest dashboard as index + dated snapshots.
site: ; rm -rf _site && mkdir -p _site && cp reports/*.html _site/ && cp reports/gold_dashboard.html _site/index.html
