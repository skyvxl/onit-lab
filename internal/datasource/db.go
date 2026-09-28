package datasource

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"github.com/jackc/pgx/v5/pgxpool"

	"onit_laba1/internal/db"
)

type Repository struct {
	pool *pgxpool.Pool
	q    *db.Queries
}

func NewPool(ctx context.Context, databaseURL string) (*pgxpool.Pool, error) {
	if strings.TrimSpace(databaseURL) == "" {
		return nil, errors.New("DATABASE_URL is required")
	}
	pool, err := pgxpool.New(ctx, databaseURL)
	if err != nil {
		return nil, fmt.Errorf("create connection pool: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping database: %w", err)
	}
	return pool, nil
}

func NewRepository(pool *pgxpool.Pool) *Repository {
	return &Repository{
		pool: pool,
		q:    db.New(pool),
	}
}

func (r *Repository) Close() {
	r.pool.Close()
}

func (r *Repository) GetMessages(ctx context.Context) ([]string, error) {
	messages, err := r.q.GetMessages(ctx)
	if err != nil {
		return nil, err
	}
	result := make([]string, len(messages))
	for i, msg := range messages {
		result[i] = msg.Text
	}
	return result, nil
}

func (r *Repository) CreateMessage(ctx context.Context, text string) error {
	err := r.q.CreateMessage(ctx, text)
	if err != nil {
		return err
	}
	return nil
}

func (r *Repository) Ping(ctx context.Context) error {
	return r.pool.Ping(ctx)
}
