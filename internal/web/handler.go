package web

import (
	"bytes"
	"context"
	"encoding/json"
	"html/template"
	"log/slog"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/gorilla/mux"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// Set at build time with -ldflags "-X onit_laba1/internal/web.buildVersion=...".
var buildVersion = "dev"

type MessageRepository interface {
	GetMessages(context.Context) ([]string, error)
	CreateMessage(context.Context, string) error
	Ping(context.Context) error
}

type responseWriterWrapper struct {
	http.ResponseWriter
	statusCode int
}

type PageData struct {
	Title    string
	Messages []string
	Version  string
	Hostname string
}

type guestbook struct {
	logger   *slog.Logger
	repo     MessageRepository
	hostname string
	version  string
}

func NewServer(logger *slog.Logger, repo MessageRepository, reg *prometheus.Registry) *http.Server {
	r := mux.NewRouter()
	RegisterRoutes(r, logger, repo, reg)
	port := os.Getenv("SERVER_PORT")
	if port == "" {
		port = "8080"
	}
	return &http.Server{
		Addr:              ":" + port,
		Handler:           r,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       5 * time.Second,
		WriteTimeout:      10 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
}

func RegisterRoutes(r *mux.Router, logger *slog.Logger, repo MessageRepository, reg *prometheus.Registry) {
	version := strings.TrimSpace(os.Getenv("APP_VERSION"))
	if version == "" {
		version = strings.TrimSpace(buildVersion)
	}
	if version == "" {
		version = "dev"
	}
	hostname, err := os.Hostname()
	if err != nil {
		logger.Error("get hostname failed", "error", err)
	}
	hostname = strings.TrimSpace(hostname)
	if hostname == "" {
		hostname = "unknown"
	}
	factory := promauto.With(reg)
	requestsIn := factory.NewCounterVec(
		prometheus.CounterOpts{
			Name: "http_requests_total",
			Help: "Total number of HTTP requests.",
		},
		[]string{"path", "method", "status"},
	)
	requestDuration := factory.NewHistogramVec(
		prometheus.HistogramOpts{
			Name: "http_request_duration_seconds",
			Help: "Duration of HTTP requests in seconds.",
		},
		[]string{"path", "method", "status"},
	)
	book := &guestbook{
		logger:   logger,
		repo:     repo,
		hostname: hostname,
		version:  version,
	}
	r.Use(book.metadataMiddleware)
	r.NotFoundHandler = book.metadataMiddleware(http.NotFoundHandler())
	r.MethodNotAllowedHandler = book.metadataMiddleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, http.StatusText(http.StatusMethodNotAllowed), http.StatusMethodNotAllowed)
	}))

	r.HandleFunc("/health", book.healthHandler).Methods(http.MethodGet)
	r.Handle("/metrics", promhttp.HandlerFor(reg, promhttp.HandlerOpts{}))

	apiRouter := r.NewRoute().Subrouter()
	apiRouter.Use(metricsMiddleware(requestsIn, requestDuration))
	apiRouter.HandleFunc("/", book.homeHandler).Methods(http.MethodGet)
	apiRouter.HandleFunc("/messages", book.messagesHandler).Methods(http.MethodPost)
	apiRouter.HandleFunc("/version", book.versionHandler).Methods(http.MethodGet)
}

func (book *guestbook) metadataMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Served-By", book.hostname)
		w.Header().Set("X-App-Version", book.version)
		next.ServeHTTP(w, r)
	})
}

func metricsMiddleware(requestsIn *prometheus.CounterVec, requestDuration *prometheus.HistogramVec) mux.MiddlewareFunc {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			start := time.Now()
			wrapper := &responseWriterWrapper{ResponseWriter: w, statusCode: http.StatusOK}
			next.ServeHTTP(wrapper, r)
			path, err := mux.CurrentRoute(r).GetPathTemplate()
			if err != nil {
				path = r.URL.Path
			}
			status := strconv.Itoa(wrapper.statusCode)
			requestsIn.WithLabelValues(path, r.Method, status).Inc()
			requestDuration.WithLabelValues(path, r.Method, status).Observe(time.Since(start).Seconds())
		})
	}
}

func (rw *responseWriterWrapper) WriteHeader(code int) {
	rw.statusCode = code
	rw.ResponseWriter.WriteHeader(code)
}

func (book *guestbook) homeHandler(w http.ResponseWriter, r *http.Request) {
	tmpl, err := template.ParseFiles("web/templates/index.html")
	if err != nil {
		book.logger.ErrorContext(r.Context(), "load page template failed", "error", err)
		http.Error(w, "не удалось открыть страницу", http.StatusInternalServerError)
		return
	}
	messages, err := book.repo.GetMessages(r.Context())
	if err != nil {
		book.logger.ErrorContext(r.Context(), "get messages failed", "error", err)
		http.Error(w, "не удалось открыть страницу", http.StatusInternalServerError)
		return
	}
	data := PageData{
		Title:    "Гостевая книга",
		Messages: messages,
		Version:  book.version,
		Hostname: book.hostname,
	}
	var body bytes.Buffer
	if err := tmpl.Execute(&body, data); err != nil {
		book.logger.ErrorContext(r.Context(), "render page failed", "error", err)
		http.Error(w, "не удалось открыть страницу", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	if _, err := w.Write(body.Bytes()); err != nil {
		book.logger.WarnContext(r.Context(), "write page failed", "error", err)
	}
}

func (book *guestbook) messagesHandler(w http.ResponseWriter, r *http.Request) {
	var request struct {
		Message string `json:"message"`
	}
	r.Body = http.MaxBytesReader(w, r.Body, 4096)
	err := json.NewDecoder(r.Body).Decode(&request)
	if err != nil {
		http.Error(w, "некорректный JSON", http.StatusBadRequest)
		return
	}
	request.Message = strings.TrimSpace(request.Message)
	if request.Message == "" {
		http.Error(w, "сообщение пустое", http.StatusBadRequest)
		return
	}
	err = book.repo.CreateMessage(r.Context(), request.Message)
	if err != nil {
		book.logger.ErrorContext(r.Context(), "create message failed", "error", err)
		http.Error(w, "не удалось сохранить сообщение", http.StatusInternalServerError)
		return
	}
	clientIP := strings.TrimSpace(r.Header.Get("X-Real-IP"))
	if clientIP == "" {
		clientIP, _, err = net.SplitHostPort(r.RemoteAddr)
		if err != nil {
			clientIP = r.RemoteAddr
		}
	}
	book.logger.InfoContext(r.Context(), "message added", "client_ip", clientIP)
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(http.StatusCreated)
	if err := json.NewEncoder(w).Encode(map[string]string{"message": request.Message}); err != nil {
		book.logger.WarnContext(r.Context(), "write response failed", "error", err)
	}
}

func (book *guestbook) healthHandler(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), time.Second)
	defer cancel()
	err := book.repo.Ping(ctx)
	if err != nil {
		http.Error(w, "не удалось подключиться к базе данных", http.StatusServiceUnavailable)
		return
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	if err := json.NewEncoder(w).Encode(map[string]string{"status": "ok"}); err != nil {
		book.logger.WarnContext(r.Context(), "write health response failed", "error", err)
	}
}

func (book *guestbook) versionHandler(w http.ResponseWriter, r *http.Request) {
	var result struct {
		Version string `json:"version"`
		Host    string `json:"host"`
	}
	result.Version = book.version
	result.Host = book.hostname

	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	if err := json.NewEncoder(w).Encode(result); err != nil {
		book.logger.WarnContext(r.Context(), "write response failed", "error", err)
	}
}
