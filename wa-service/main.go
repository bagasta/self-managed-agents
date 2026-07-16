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

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}

	pythonWebhook := os.Getenv("PYTHON_WEBHOOK_URL")
	if pythonWebhook == "" {
		pythonWebhook = "http://localhost:8000/v1/channels/wa/incoming"
	}

	storeDir := os.Getenv("WA_STORE_DIR")
	if storeDir == "" {
		storeDir = "wa-store"
	}

	log.Printf("wa-service starting on :%s", port)
	log.Printf("python webhook: %s", pythonWebhook)
	log.Printf("store dir: %s", storeDir)

	dm, err := NewDeviceManager(pythonWebhook, storeDir)
	if err != nil {
		log.Fatalf("device manager init: %v", err)
	}
	defer dm.Close()

	h := NewHandlers(dm)

	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"status":"ok"}`)
	})
	mux.HandleFunc("POST /devices", h.createDevice)
	mux.HandleFunc("GET /devices/{id}/qr", h.getQR)
	mux.HandleFunc("POST /devices/{id}/qr", h.refreshQR)
	mux.HandleFunc("GET /devices/{id}/status", h.getStatus)
	mux.HandleFunc("POST /devices/{id}/send", h.sendMessage)
	mux.HandleFunc("POST /devices/{id}/send-contact", h.sendContactMessage)
	mux.HandleFunc("POST /devices/{id}/typing/start", h.startTyping)
	mux.HandleFunc("POST /devices/{id}/typing/stop", h.stopTyping)
	mux.HandleFunc("POST /devices/{id}/send-image", h.sendImageMessage)
	mux.HandleFunc("POST /devices/{id}/send-document", h.sendDocumentMessage)
	mux.HandleFunc("POST /devices/{id}/resolve-phones", h.resolvePhones)
	mux.HandleFunc("DELETE /devices/{id}", h.deleteDevice)

	server := &http.Server{
		Addr:              ":" + port,
		Handler:           mux,
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
