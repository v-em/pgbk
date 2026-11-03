UV ?= uv
PYTHON ?= $(UV) run --frozen python
HELM ?= helm
DIST_DIR ?= dist

.PHONY: schema test chart package check
schema:
	$(PYTHON) scripts/schema.py
test:
	$(PYTHON) -m pytest
chart:
	$(HELM) lint charts/pgbk -f examples/values.yaml
	$(HELM) template pgbk charts/pgbk -n backups -f examples/values.yaml > /tmp/pgbk-chart.yaml
package:
	$(UV) build --out-dir $(DIST_DIR)
	$(HELM) package charts/pgbk -d $(DIST_DIR)
check: test chart
	$(PYTHON) scripts/check_docs.py
