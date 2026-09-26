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
	app := di.NewApp(appContext, logger)
	if err := app.Run(); err != nil {
		logger.Error("application stopped with error", "error", err)
		return 1
	}
	return 0
}
