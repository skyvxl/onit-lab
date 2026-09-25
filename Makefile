.PHONY: all build run fmt lint

all: build

build:
	go build -o bin/server cmd/server/main.go

run:
	./bin/server

fmt:
	go fmt ./...

lint:
	golangci-lint run
