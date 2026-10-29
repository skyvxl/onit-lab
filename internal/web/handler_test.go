package web

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"

	"github.com/gorilla/mux"
	"github.com/prometheus/client_golang/prometheus"
)

// Keep persistence real in memory while replacing the external PostgreSQL connection.
type memoryRepository struct {
	mu       sync.Mutex
	messages []string
	pingErr  error
}

func (repo *memoryRepository) GetMessages(context.Context) ([]string, error) {
	repo.mu.Lock()
	defer repo.mu.Unlock()
	return append([]string(nil), repo.messages...), nil
}

func (repo *memoryRepository) CreateMessage(_ context.Context, message string) error {
	repo.mu.Lock()
	defer repo.mu.Unlock()
	repo.messages = append([]string{message}, repo.messages...)
	return nil
}

func (repo *memoryRepository) Ping(context.Context) error { return repo.pingErr }

func testRouter(t *testing.T, repo MessageRepository, output io.Writer) *mux.Router {
	t.Helper()
	t.Chdir("../..")
	router := mux.NewRouter()
	RegisterRoutes(router, slog.New(slog.NewJSONHandler(output, nil)), repo, prometheus.NewRegistry())
	return router
}

func TestVersionMetadataFromFirstRequest(t *testing.T) {
	t.Setenv("APP_VERSION", "  v2.3.4  ")
	router := testRouter(t, &memoryRepository{}, io.Discard)
	hostname, err := os.Hostname()
	if err != nil {
		t.Fatal(err)
	}
	response := httptest.NewRecorder()
	router.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/version", nil))
	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", response.Code, response.Body)
	}
	var body struct {
		Version string `json:"version"`
		Host    string `json:"host"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body.Version != "v2.3.4" || body.Host != hostname {
		t.Errorf("metadata = %+v, want version v2.3.4 and host %q", body, hostname)
	}
	if response.Header().Get("X-App-Version") != body.Version || response.Header().Get("X-Served-By") != hostname {
		t.Errorf("metadata headers missing or inconsistent: %v", response.Header())
	}
}

func TestVersionCapturedAtRegistrationAndConcurrentRequests(t *testing.T) {
	t.Setenv("APP_VERSION", "registered-version")
	router := testRouter(t, &memoryRepository{}, io.Discard)
	t.Setenv("APP_VERSION", "changed-after-registration")
	var requests sync.WaitGroup
	for range 32 {
		requests.Go(func() {
			response := httptest.NewRecorder()
			router.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/version", nil))
			var body map[string]string
			if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
				t.Error(err)
				return
			}
			if body["version"] != "registered-version" || body["host"] == "" {
				t.Errorf("metadata changed or empty: %v", body)
			}
		})
	}
	requests.Wait()
}

func TestVersionDefaultsToDev(t *testing.T) {
	t.Setenv("APP_VERSION", " \t ")
	router := testRouter(t, &memoryRepository{}, io.Discard)
	response := httptest.NewRecorder()
	router.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/version", nil))
	var body map[string]string
	if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body["version"] != "dev" {
		t.Errorf("version = %q, want dev", body["version"])
	}
}

func TestVersionUsesCompiledDefaultUnlessEnvironmentOverridesIt(t *testing.T) {
	original := buildVersion
	buildVersion = "baked-release"
	t.Cleanup(func() { buildVersion = original })
	for _, test := range []struct{ name, environment, want string }{
		{"compiled default", " \t ", "baked-release"},
		{"environment override", " deploy-release ", "deploy-release"},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Setenv("APP_VERSION", test.environment)
			router := testRouter(t, &memoryRepository{}, io.Discard)
			response := httptest.NewRecorder()
			router.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/version", nil))
			var body map[string]string
			if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
				t.Fatal(err)
			}
			if body["version"] != test.want {
				t.Errorf("version = %q, want %q", body["version"], test.want)
			}
		})
	}
}

func TestMetadataHeadersOnEveryResponse(t *testing.T) {
	t.Setenv("APP_VERSION", "v2")
	router := testRouter(t, &memoryRepository{}, io.Discard)
	for _, request := range []struct{ method, path, body string }{
		{http.MethodGet, "/", ""},
		{http.MethodGet, "/health", ""},
		{http.MethodGet, "/metrics", ""},
		{http.MethodPost, "/messages", `{"message":"hello"}`},
		{http.MethodPost, "/messages", `{"message":"  "}`},
		{http.MethodGet, "/missing", ""},
		{http.MethodDelete, "/health", ""},
	} {
		response := httptest.NewRecorder()
		router.ServeHTTP(response, httptest.NewRequest(request.method, request.path, strings.NewReader(request.body)))
		if response.Header().Get("X-App-Version") != "v2" || response.Header().Get("X-Served-By") == "" {
			t.Errorf("%s %s: missing metadata headers: %v", request.method, request.path, response.Header())
		}
	}
}

func TestHealthReportsDatabaseAvailability(t *testing.T) {
	for _, test := range []struct {
		name   string
		err    error
		status int
	}{
		{"healthy", nil, http.StatusOK},
		{"database unavailable", errors.New("database unavailable"), http.StatusServiceUnavailable},
	} {
		t.Run(test.name, func(t *testing.T) {
			router := testRouter(t, &memoryRepository{pingErr: test.err}, io.Discard)
			response := httptest.NewRecorder()
			router.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/health", nil))
			if response.Code != test.status {
				t.Fatalf("status = %d, want %d", response.Code, test.status)
			}
			if test.err == nil {
				var body map[string]string
				if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil || body["status"] != "ok" {
					t.Errorf("healthy response = %q, want JSON status ok", response.Body)
				}
				if response.Header().Get("Content-Type") != "application/json; charset=utf-8" {
					t.Errorf("healthy response content type = %q", response.Header().Get("Content-Type"))
				}
			}
		})
	}
}

func TestPostPersistsMessageAndPageShowsReplica(t *testing.T) {
	t.Setenv("APP_VERSION", "release-2")
	router := testRouter(t, &memoryRepository{}, io.Discard)
	post := httptest.NewRecorder()
	router.ServeHTTP(post, httptest.NewRequest(http.MethodPost, "/messages", strings.NewReader(`{"message":"  <hello>  "}`)))
	if post.Code != http.StatusCreated || post.Body.String() != "{\"message\":\"\\u003chello\\u003e\"}\n" {
		t.Fatalf("POST response = %d %s", post.Code, post.Body)
	}
	page := httptest.NewRecorder()
	router.ServeHTTP(page, httptest.NewRequest(http.MethodGet, "/", nil))
	if page.Code != http.StatusOK || !strings.Contains(page.Body.String(), "&lt;hello&gt;") {
		t.Fatalf("saved message not safely rendered: %d %s", page.Code, page.Body)
	}
	body := page.Body.String()
	footer := strings.Index(body, "<footer")
	if footer < 0 || !strings.Contains(body[footer:], "release-2") || !strings.Contains(body[footer:], page.Header().Get("X-Served-By")) {
		t.Error("footer does not show application version and replica")
	}
	metrics := httptest.NewRecorder()
	router.ServeHTTP(metrics, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if !strings.Contains(metrics.Body.String(), `http_requests_total{method="POST",path="/messages",status="201"} 1`) {
		t.Error("successful POST is not counted in metrics")
	}
}

func TestRequestLogUsesClientAddress(t *testing.T) {
	for _, test := range []struct{ name, forwarded, want string }{
		{"proxy address", "203.0.113.7", "203.0.113.7"},
		{"direct request", "", "192.0.2.5"},
	} {
		t.Run(test.name, func(t *testing.T) {
			var logs bytes.Buffer
			router := testRouter(t, &memoryRepository{}, &logs)
			request := httptest.NewRequest(http.MethodPost, "/messages", strings.NewReader(`{"message":"hello"}`))
			request.RemoteAddr = "192.0.2.5:12345"
			request.Header.Set("X-Real-IP", test.forwarded)
			router.ServeHTTP(httptest.NewRecorder(), request)
			var record map[string]any
			if err := json.Unmarshal(logs.Bytes(), &record); err != nil || record["client_ip"] != test.want {
				t.Errorf("request log = %q, want client_ip %s", logs.String(), test.want)
			}
		})
	}
}
