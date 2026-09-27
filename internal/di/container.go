package di

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"

	"onit_laba1/internal/datasource"
	"onit_laba1/internal/web"
)

type app struct {
	ctx    context.Context
	logger *slog.Logger
	server *http.Server
	repo   *datasource.Repository
}

func NewApp(ctx context.Context, logger *slog.Logger) (*app, error) {
	databaseURL := strings.TrimSpace(os.Getenv("DATABASE_URL"))
	if databaseURL == "" {
		return nil, errors.New("DATABASE_URL is required")
	}
	initCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	pool, err := datasource.NewPool(initCtx, databaseURL)
	if err != nil {
		return nil, fmt.Errorf("initialize database: %w", err)
	}
	reg := prometheus.NewRegistry()
	reg.MustRegister(
		collectors.NewGoCollector(),
		collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}),
	)
	repo := datasource.NewRepository(pool)
	return &app{
		ctx:    ctx,
		logger: logger,
		server: web.NewServer(logger, repo, reg),
		repo:   repo,
	}, nil
}

func (a *app) Run() error {
	defer a.repo.Close()
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
