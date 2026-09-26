package web

import (
	"bytes"
	"encoding/json"
	"html/template"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/mux"
)

type PageData struct {
	Title    string
	Messages []string
}

type guestbook struct {
	logger   *slog.Logger
	mu       sync.RWMutex
	messages []string
}

func NewServer(logger *slog.Logger) *http.Server {
	r := mux.NewRouter()
	RegisterRoutes(r, logger)
	return &http.Server{
		Addr:              ":8080",
		Handler:           r,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       5 * time.Second,
		WriteTimeout:      10 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
}

func RegisterRoutes(r *mux.Router, logger *slog.Logger) {
	book := &guestbook{logger: logger}
	r.HandleFunc("/", book.homeHandler).Methods(http.MethodGet)
	r.HandleFunc("/messages", book.messagesHandler).Methods(http.MethodPost)
}

func (book *guestbook) homeHandler(w http.ResponseWriter, r *http.Request) {
	tmpl, err := template.ParseFiles("web/templates/index.html")
	if err != nil {
		book.logger.ErrorContext(r.Context(), "load page template failed", "error", err)
		http.Error(w, "не удалось открыть страницу", http.StatusInternalServerError)
		return
	}
	book.mu.RLock()
	messages := append([]string(nil), book.messages...)
	book.mu.RUnlock()
	data := PageData{
		Title:    "Гостевая книга",
		Messages: messages,
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
	book.mu.Lock()
	book.messages = append(book.messages, request.Message)
	book.mu.Unlock()
	book.logger.InfoContext(r.Context(), "message added")
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	if err := json.NewEncoder(w).Encode(map[string]string{
		"message": request.Message,
	}); err != nil {
		book.logger.WarnContext(r.Context(), "write response failed", "error", err)
	}
}
