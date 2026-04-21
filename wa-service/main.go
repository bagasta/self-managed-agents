package main

import (
	"fmt"
	"log"
	"net/http"
	"os"
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
	mux.HandleFunc("GET /devices/{id}/status", h.getStatus)
	mux.HandleFunc("POST /devices/{id}/send", h.sendMessage)
	mux.HandleFunc("POST /devices/{id}/send-image", h.sendImageMessage)
	mux.HandleFunc("DELETE /devices/{id}", h.deleteDevice)

	if err = http.ListenAndServe(":"+port, mux); err != nil {
		log.Fatalf("server: %v", err)
	}
}
