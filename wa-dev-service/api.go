package main

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
)

type API struct {
	wa    *WhatsAppClient
	store *ConnectionStore
}

func NewAPI(wa *WhatsAppClient, store *ConnectionStore) *API {
	return &API{wa: wa, store: store}
}

// GET /status
func (a *API) GetStatus(w http.ResponseWriter, r *http.Request) {
	status, phone, qr := a.wa.GetStatus()
	writeJSON(w, map[string]interface{}{
		"status":       status,
		"phone_number": phone,
		"qr":           qr,
	})
}

// POST /connect-wa
func (a *API) ConnectWhatsApp(w http.ResponseWriter, r *http.Request) {
	qr, err := a.wa.Connect()
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	writeJSON(w, map[string]interface{}{"qr": qr})
}

// GET /connections
func (a *API) ListConnections(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, a.store.List())
}

// DELETE /connections/{phone}
func (a *API) DeleteConnection(w http.ResponseWriter, r *http.Request) {
	phone := r.PathValue("phone")
	if phone == "" {
		http.Error(w, "phone required", http.StatusBadRequest)
		return
	}
	if err := a.store.Delete(phone); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// POST /send/text
// Body: {"to": "+62xxx or chat_id@s.whatsapp.net", "text": "..."}
func (a *API) SendText(w http.ResponseWriter, r *http.Request) {
	var body struct {
		To   string `json:"to"`
		Text string `json:"text"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.To == "" || body.Text == "" {
		http.Error(w, `{"error":"to and text are required"}`, http.StatusBadRequest)
		return
	}
	if err := a.wa.SendText(body.To, body.Text); err != nil {
		http.Error(w, fmt.Sprintf(`{"error":"%s"}`, err.Error()), http.StatusInternalServerError)
		return
	}
	writeJSON(w, map[string]string{"status": "sent"})
}

// POST /send/image
// Body: {"to": "...", "image": "<base64>", "caption": "...", "mimetype": "image/jpeg"}
func (a *API) SendImage(w http.ResponseWriter, r *http.Request) {
	var body struct {
		To       string `json:"to"`
		Image    string `json:"image"`    // base64
		Caption  string `json:"caption"`
		Mimetype string `json:"mimetype"` // default: image/jpeg
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.To == "" || body.Image == "" {
		http.Error(w, `{"error":"to and image (base64) are required"}`, http.StatusBadRequest)
		return
	}
	imgData, err := base64.StdEncoding.DecodeString(body.Image)
	if err != nil {
		http.Error(w, `{"error":"image must be valid base64"}`, http.StatusBadRequest)
		return
	}
	if err := a.wa.SendImage(body.To, imgData, body.Caption, body.Mimetype); err != nil {
		http.Error(w, fmt.Sprintf(`{"error":"%s"}`, err.Error()), http.StatusInternalServerError)
		return
	}
	writeJSON(w, map[string]string{"status": "sent"})
}

// POST /send/image/url
// Body: {"to": "...", "url": "https://...", "caption": "...", "mimetype": "image/jpeg"}
func (a *API) SendImageURL(w http.ResponseWriter, r *http.Request) {
	var body struct {
		To       string `json:"to"`
		URL      string `json:"url"`
		Caption  string `json:"caption"`
		Mimetype string `json:"mimetype"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.To == "" || body.URL == "" {
		http.Error(w, `{"error":"to and url are required"}`, http.StatusBadRequest)
		return
	}
	imgData, err := downloadURL(body.URL)
	if err != nil {
		http.Error(w, fmt.Sprintf(`{"error":"download url failed: %s"}`, err.Error()), http.StatusBadRequest)
		return
	}
	if err := a.wa.SendImage(body.To, imgData, body.Caption, body.Mimetype); err != nil {
		http.Error(w, fmt.Sprintf(`{"error":"%s"}`, err.Error()), http.StatusInternalServerError)
		return
	}
	writeJSON(w, map[string]string{"status": "sent"})
}

// POST /send/document
// Body: {"to": "...", "data": "<base64>", "filename": "file.pdf", "caption": "...", "mimetype": "application/pdf"}
func (a *API) SendDocument(w http.ResponseWriter, r *http.Request) {
	var body struct {
		To       string `json:"to"`
		Data     string `json:"data"`     // base64
		Filename string `json:"filename"`
		Caption  string `json:"caption"`
		Mimetype string `json:"mimetype"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.To == "" || body.Data == "" {
		http.Error(w, `{"error":"to and data (base64) are required"}`, http.StatusBadRequest)
		return
	}
	docData, err := base64.StdEncoding.DecodeString(body.Data)
	if err != nil {
		http.Error(w, `{"error":"data must be valid base64"}`, http.StatusBadRequest)
		return
	}
	if err := a.wa.SendDocument(body.To, docData, body.Filename, body.Caption, body.Mimetype); err != nil {
		http.Error(w, fmt.Sprintf(`{"error":"%s"}`, err.Error()), http.StatusInternalServerError)
		return
	}
	writeJSON(w, map[string]string{"status": "sent"})
}

// POST /send/document/url
// Body: {"to": "...", "url": "https://...", "filename": "file.pdf", "caption": "...", "mimetype": "application/pdf"}
func (a *API) SendDocumentURL(w http.ResponseWriter, r *http.Request) {
	var body struct {
		To       string `json:"to"`
		URL      string `json:"url"`
		Filename string `json:"filename"`
		Caption  string `json:"caption"`
		Mimetype string `json:"mimetype"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.To == "" || body.URL == "" {
		http.Error(w, `{"error":"to and url are required"}`, http.StatusBadRequest)
		return
	}
	docData, err := downloadURL(body.URL)
	if err != nil {
		http.Error(w, fmt.Sprintf(`{"error":"download url failed: %s"}`, err.Error()), http.StatusBadRequest)
		return
	}
	if err := a.wa.SendDocument(body.To, docData, body.Filename, body.Caption, body.Mimetype); err != nil {
		http.Error(w, fmt.Sprintf(`{"error":"%s"}`, err.Error()), http.StatusInternalServerError)
		return
	}
	writeJSON(w, map[string]string{"status": "sent"})
}

// POST /resolve-phones
// Body: {"phones": ["+628xxx"]}
// Response: {"resolved": {"628xxx": "628xxx@s.whatsapp.net"}}
func (a *API) ResolvePhones(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Phones []string `json:"phones"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || len(req.Phones) == 0 {
		http.Error(w, `{"error":"phones array required"}`, http.StatusBadRequest)
		return
	}
	resolved, err := a.wa.ResolvePhones(req.Phones)
	if err != nil {
		http.Error(w, fmt.Sprintf(`{"error":"%s"}`, err.Error()), http.StatusServiceUnavailable)
		return
	}
	writeJSON(w, map[string]interface{}{"resolved": resolved})
}

func writeJSON(w http.ResponseWriter, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(v)
}

func downloadURL(url string) ([]byte, error) {
	resp, err := http.Get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		return nil, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	return io.ReadAll(resp.Body)
}
