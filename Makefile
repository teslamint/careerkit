.PHONY: build docker-build clean

VARIANT ?= example
FORMAT ?= all


docker-build:
	docker compose build

build: docker-build
	docker compose run --rm resume $(VARIANT) $(FORMAT)

clean:
	rm -rf private/build/*

.PHONY: install-hooks refresh-guard-cache

install-hooks:
	git config core.hooksPath .githooks
	$(MAKE) refresh-guard-cache

refresh-guard-cache:
	-uv run python -c "from careerkit.publish_guard import build_vocabulary, _merge_base_ref; import os; base = _merge_base_ref(); (base and build_vocabulary(base))"
