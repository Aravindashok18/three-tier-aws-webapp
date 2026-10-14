PY := .venv/bin/python

.PHONY: venv test lint run package

venv:
	python3.12 -m venv .venv
	$(PY) -m pip install -r requirements-dev.txt

test:
	$(PY) -m pytest

lint:
	.venv/bin/cfn-lint infra/three-tier.yaml

# local run on SQLite, http://127.0.0.1:5000
run:
	DATABASE_URL=sqlite:///local.db $(PY) -m flask --app app.wsgi run

package:
	scripts/deploy.sh $(STACK) $(LABEL)
