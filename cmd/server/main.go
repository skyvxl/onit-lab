package main

import (
	"context"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"onit_laba1/internal/di"
)

func main() {
	os.Exit(run())
}

func run() int {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	appContext, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	app, err := di.NewApp(appContext, logger)
	if err != nil {
		logger.Error("application initialization failed", "error", err)
		return 1
	}
	if err := app.Run(); err != nil {
		logger.Error("application stopped with error", "error", err)
		return 1
	}
	return 0
}
