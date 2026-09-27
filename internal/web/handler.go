package web

import (
	"bytes"
	"context"
	"encoding/json"
	"html/template"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/gorilla/mux"
)

type MessageRepository interface {
	GetMessages(context.Context) ([]string, error)
	CreateMessage(context.Context, string) error
}

type PageData struct {
	Title    string
	Messages []string
}

type guestbook struct {
	logger *slog.Logger
	repo   MessageRepository
}

func NewServer(logger *slog.Logger, repo MessageRepository) *http.Server {
	r := mux.NewRouter()
	RegisterRoutes(r, logger, repo)
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

func RegisterRoutes(r *mux.Router, logger *slog.Logger, repo MessageRepository) {
	book := &guestbook{logger: logger, repo: repo}
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
	messages, err := book.repo.GetMessages(r.Context())
	if err != nil {
		book.logger.ErrorContext(r.Context(), "get messages failed", "error", err)
		http.Error(w, "не удалось открыть страницу", http.StatusInternalServerError)
		return
	}
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
	err = book.repo.CreateMessage(r.Context(), request.Message)
	if err != nil {
		book.logger.ErrorContext(r.Context(), "create message failed", "error", err)
		http.Error(w, "не удалось сохранить сообщение", http.StatusInternalServerError)
		return
	}
	book.logger.InfoContext(r.Context(), "message added")
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(http.StatusCreated)
	if err := json.NewEncoder(w).Encode(map[string]string{"message": request.Message}); err != nil {
		book.logger.WarnContext(r.Context(), "write response failed", "error", err)
	}
}
