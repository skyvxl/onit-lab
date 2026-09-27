-include .env
export

all: run-dev

build:
	go build -o bin/server cmd/server/main.go

run:
	./bin/server

run-dev:
	go run cmd/server/main.go

fmt:
	go fmt ./...

lint:
	golangci-lint run

test:
	go test ./...

migrate-up:
	docker compose run --build --rm migrate up

migrate-down:
	docker compose run --build --rm migrate down

.PHONY: all build run run-dev fmt lint test migrate-up migrate-down
