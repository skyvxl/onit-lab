-include .env.dev
export

all: run-dev

build:
	go build -o bin/server cmd/server/main.go

run:
	./bin/server

run-dev:
	go run cmd/server/main.go

lint:
	golangci-lint run

lint-fix:
	golangci-lint run --fix

test:
	go test ./...

migrate-up:
	docker compose run --build --rm migrate up

migrate-down:
	docker compose run --build --rm migrate down

.PHONY: all build run run-dev lint lint-fix test migrate-up migrate-down
