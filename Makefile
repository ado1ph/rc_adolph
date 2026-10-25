PYTHON ?= python3

.PHONY: test demo demo-production serve supplier
test:
	$(PYTHON) -m unittest discover -s tests -v

demo:
	$(PYTHON) -m notifier demo

demo-production:
	$(PYTHON) -m notifier.production.demo

serve:
	NOTIFY_TOKEN=local-demo-secret NOTIFY_ALLOWED_ORIGINS=http://127.0.0.1:9100 NOTIFY_ALLOW_LOOPBACK=1 $(PYTHON) -m notifier serve

supplier:
	$(PYTHON) -m notifier supplier

test-production:
	@test -n "$(NOTIFY_TEST_DATABASE_URL)" || (echo "Set NOTIFY_TEST_DATABASE_URL to an isolated PostgreSQL"; exit 1)
	$(PYTHON) -m unittest tests.test_production -v

production-api:
	$(PYTHON) -m notifier.production api

production-worker:
	$(PYTHON) -m notifier.production worker

production-migrate:
	$(PYTHON) -m notifier.production migrate

lint:
	$(PYTHON) -m ruff check notifier/production tests/test_production.py tests/test_validation.py scripts/init_config.py
