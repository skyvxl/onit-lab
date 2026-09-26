.PHONY: all build run run-dev fmt lint

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
