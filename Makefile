UV ?= uv
PYTHON ?= $(UV) run --frozen python
HELM ?= helm

.PHONY: schema test chart package check
schema:
	$(PYTHON) scripts/schema.py
test:
	$(PYTHON) -m pytest
chart:
	$(HELM) lint charts/pgbk -f examples/values.yaml
	$(HELM) template pgbk charts/pgbk -n backups -f examples/values.yaml > /tmp/pgbk-chart.yaml
package:
	$(UV) build
	$(HELM) package charts/pgbk -d dist
check: test chart
	$(PYTHON) scripts/check_docs.py
