package di

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"time"

	"onit_laba1/internal/web"
)

type app struct {
	ctx    context.Context
	logger *slog.Logger
	server *http.Server
}

func NewApp(ctx context.Context, logger *slog.Logger) *app {
	return &app{
		ctx:    ctx,
		logger: logger,
		server: web.NewServer(logger),
	}
}

func (a *app) Run() error {
	return a.runHTTPServer()
}

func (a *app) runHTTPServer() error {
	serveErr := make(chan error, 1)
	go func() {
		serveErr <- a.server.ListenAndServe()
	}()
	a.logger.Info("server starting", "address", a.server.Addr)

	select {
	case err := <-serveErr:
		return fmt.Errorf("serve HTTP: %w", err)
	case <-a.ctx.Done():
		a.logger.Info("shutdown started")
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := a.server.Shutdown(shutdownCtx); err != nil {
		_ = a.server.Close()
		<-serveErr
		return fmt.Errorf("shutdown HTTP server: %w", err)
	}
	if err := <-serveErr; err != nil && !errors.Is(err, http.ErrServerClosed) {
		return fmt.Errorf("serve HTTP: %w", err)
	}
	a.logger.Info("shutdown completed")
	return nil
}
