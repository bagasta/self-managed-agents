package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
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
		MainAPIKey: getEnv("MAIN_API_KEY", getEnv("API_KEY", "")),
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

	router := NewRouter(cfg.MainAPIURL, cfg.MainAPIKey, store, cfg.WebhookURL, getEnv("AUTO_AGENT_ID", ""))
	defer router.Close()

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
	mux.HandleFunc("POST /send/contact", api.SendContact)
	mux.HandleFunc("POST /send/image", api.SendImage)
	mux.HandleFunc("POST /send/image/url", api.SendImageURL)
	mux.HandleFunc("POST /send/document", api.SendDocument)
	mux.HandleFunc("POST /send/document/url", api.SendDocumentURL)
	mux.HandleFunc("POST /resolve-phones", api.ResolvePhones)

	// Dashboard
	mux.Handle("/", http.FileServer(http.Dir("./dashboard")))

	log.Printf("wa-dev-service starting on :%s", cfg.Port)
	log.Printf("main API: %s", cfg.MainAPIURL)
	if cfg.WebhookURL != "" {
		log.Printf("webhook: %s", cfg.WebhookURL)
	}

	server := &http.Server{
		Addr:              ":" + cfg.Port,
		Handler:           corsMiddleware(mux),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      120 * time.Second,
		IdleTimeout:       60 * time.Second,
		MaxHeaderBytes:    1 << 20,
	}

	shutdownSignal, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	serverErr := make(chan error, 1)
	go func() { serverErr <- server.ListenAndServe() }()

	select {
	case <-shutdownSignal.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		if err = server.Shutdown(shutdownCtx); err != nil {
			log.Printf("server graceful shutdown: %v", err)
		}
	case err = <-serverErr:
		if !errors.Is(err, http.ErrServerClosed) {
			log.Printf("server: %v", err)
		}
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
