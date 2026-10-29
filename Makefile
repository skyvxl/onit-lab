all: run-dev

build:
	go build -o bin/server ./cmd/server

run:
	set -a; if [ -f .env.dev ]; then . ./.env.dev; fi; set +a; ./bin/server

run-dev:
	set -a; if [ -f .env.dev ]; then . ./.env.dev; fi; set +a; go run ./cmd/server

test:
	go test ./...
	python3 -m unittest discover -s tests -v

lint:
	go vet ./...

lint-fix:
	gofmt -w cmd internal

init:
	python3 scripts/init-env.py

up: init
	docker compose up -d --build --wait --wait-timeout 120

down:
	docker compose down

smoke:
	python3 scripts/check-http.py smoke

rate-check:
	python3 scripts/check-http.py rate

gitea:
	python3 scripts/setup-gitea.py

migrate-up:
	docker compose run --build --rm migrate up

migrate-down:
	docker compose run --build --rm migrate down

.PHONY: all build run run-dev test lint lint-fix init up down smoke rate-check gitea migrate-up migrate-down
