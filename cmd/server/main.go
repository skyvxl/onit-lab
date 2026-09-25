package main

import (
	"encoding/json"
	"html/template"
	"net/http"

	"github.com/gorilla/mux"
)

type PageData struct {
	Title    string
	Messages []string
}

func main() {
	var Messages []string
	r := mux.NewRouter()
	r.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		tmpl, err := template.ParseFiles("web/templates/index.html")
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		data := PageData{
			Title:    "Встроенные шаблоны Go",
			Messages: Messages,
		}
		tmpl.Execute(w, data)
	}).Methods("GET")
	r.HandleFunc("/messages", func(w http.ResponseWriter, r *http.Request) {
		var request struct {
			Message string `json:"message"`
		}
		err := json.NewDecoder(r.Body).Decode(&request)
		if err != nil {
			http.Error(w, "некорректный JSON", http.StatusBadRequest)
			return
		}
		if request.Message == "" {
			http.Error(w, "сообщение пустое", http.StatusBadRequest)
			return
		}
		Messages = append(Messages, request.Message)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		json.NewEncoder(w).Encode(map[string]string{
			"message": request.Message,
		})
	}).Methods("POST")

	if err := http.ListenAndServe(":8080", r); err != nil {
		panic(err)
	}
}
