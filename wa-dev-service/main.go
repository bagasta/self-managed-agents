package main

import (
	"fmt"
	"log"
	"net/http"
	"os"
)

type Config struct {
	Port       string
	StoreDir   string
	MainAPIURL string
	MainAPIKey string
	StoreFile  string
	WebhookURL string // optional: forward raw incoming messages to this URL
}

func main() {
	cfg := Config{
		Port:       getEnv("PORT", "8081"),
		StoreDir:   getEnv("WA_DEV_STORE_DIR", "wa-dev-store"),
		MainAPIURL: getEnv("MAIN_API_URL", "http://localhost:8000"),
		MainAPIKey: getEnv("MAIN_API_KEY", ""),
		StoreFile:  getEnv("CONNECTIONS_FILE", "connections.json"),
		WebhookURL: getEnv("WEBHOOK_URL", ""),
	}

	if cfg.MainAPIKey == "" {
		log.Fatal("MAIN_API_KEY is required")
	}

	store, err := NewConnectionStore(cfg.StoreFile)
	if err != nil {
		log.Fatalf("store init: %v", err)
	}

	router := NewRouter(cfg.MainAPIURL, cfg.MainAPIKey, store, cfg.WebhookURL)

	wa, err := NewWhatsAppClient(cfg.StoreDir, router.HandleMessage)
	if err != nil {
		log.Fatalf("whatsapp init: %v", err)
	}
	defer wa.Close()

	router.SetWA(wa)

	api := NewAPI(wa, store)

	mux := http.NewServeMux()

	// System
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"status":"ok"}`)
	})

	// WhatsApp connection
	mux.HandleFunc("GET /status", api.GetStatus)
	mux.HandleFunc("POST /connect-wa", api.ConnectWhatsApp)

	// User-agent connections
	mux.HandleFunc("GET /connections", api.ListConnections)
	mux.HandleFunc("DELETE /connections/{phone}", api.DeleteConnection)

	// Send messages
	mux.HandleFunc("POST /send/text", api.SendText)
	mux.HandleFunc("POST /send/image", api.SendImage)
	mux.HandleFunc("POST /send/image/url", api.SendImageURL)
	mux.HandleFunc("POST /send/document", api.SendDocument)
	mux.HandleFunc("POST /send/document/url", api.SendDocumentURL)

	// Dashboard
	mux.Handle("/", http.FileServer(http.Dir("./dashboard")))

	log.Printf("wa-dev-service starting on :%s", cfg.Port)
	log.Printf("main API: %s", cfg.MainAPIURL)
	if cfg.WebhookURL != "" {
		log.Printf("webhook: %s", cfg.WebhookURL)
	}

	if err = http.ListenAndServe(":"+cfg.Port, corsMiddleware(mux)); err != nil {
		log.Fatalf("server: %v", err)
	}
}

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func corsMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}
