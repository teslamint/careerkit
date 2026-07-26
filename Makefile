.PHONY: build docker-build clean

VARIANT ?= example
FORMAT ?= all


docker-build:
	docker compose build

build: docker-build
	docker compose run --rm resume $(VARIANT) $(FORMAT)

clean:
	rm -rf private/build/*
