package main

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
)

type Handlers struct {
	dm *DeviceManager
}

func NewHandlers(dm *DeviceManager) *Handlers {
	return &Handlers{dm: dm}
}

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

// POST /devices
// Body: {"device_id": "<uuid>"}
// Response: {"device_id": "...", "qr_image": "<base64 png>", "status": "waiting_qr|connected"}
func (h *Handlers) createDevice(w http.ResponseWriter, r *http.Request) {
	var req struct {
		DeviceID string `json:"device_id"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.DeviceID == "" {
		writeError(w, http.StatusBadRequest, "device_id required")
		return
	}

	qrImage, err := h.dm.CreateDevice(req.DeviceID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}

	status := StatusWaitingQR
	if qrImage == "" {
		status = StatusConnected
	}

	writeJSON(w, http.StatusCreated, map[string]interface{}{
		"device_id": req.DeviceID,
		"qr_image":  qrImage,
		"status":    status,
	})
}

// GET /devices/{id}/qr
// Response: {"device_id": "...", "qr_image": "<base64>", "status": "..."}
func (h *Handlers) getQR(w http.ResponseWriter, r *http.Request) {
	deviceID := r.PathValue("id")
	if deviceID == "" {
		writeError(w, http.StatusBadRequest, "device id required")
		return
	}

	qr, status, err := h.dm.GetQR(deviceID)
	if err != nil {
		writeError(w, http.StatusNotFound, err.Error())
		return
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"device_id": deviceID,
		"qr_image":  qr,
		"status":    status,
	})
}

// GET /devices/{id}/status
// Response: {"device_id": "...", "status": "...", "phone_number": "..."}
func (h *Handlers) getStatus(w http.ResponseWriter, r *http.Request) {
	deviceID := r.PathValue("id")
	if deviceID == "" {
		writeError(w, http.StatusBadRequest, "device id required")
		return
	}

	status, phone, err := h.dm.GetStatus(deviceID)
	if err != nil {
		writeError(w, http.StatusNotFound, err.Error())
		return
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"device_id":    deviceID,
		"status":       status,
		"phone_number": fmt.Sprintf("+%s", phone),
	})
}

// POST /devices/{id}/send
// Body: {"to": "+628xxx", "message": "..."}
func (h *Handlers) sendMessage(w http.ResponseWriter, r *http.Request) {
	deviceID := r.PathValue("id")
	if deviceID == "" {
		writeError(w, http.StatusBadRequest, "device id required")
		return
	}

	var req struct {
		To      string `json:"to"`
		Message string `json:"message"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.To == "" || req.Message == "" {
		writeError(w, http.StatusBadRequest, "to and message required")
		return
	}

	preview := req.Message
	if len(preview) > 80 {
		preview = preview[:80] + "..."
	}
	log.Printf("[%s] send request → %s: %q", deviceID, req.To, preview)

	if err := h.dm.SendMessage(deviceID, req.To, req.Message); err != nil {
		log.Printf("[%s] send FAILED → %s: %v", deviceID, req.To, err)
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}

	log.Printf("[%s] send OK → %s", deviceID, req.To)
	writeJSON(w, http.StatusOK, map[string]string{"status": "sent"})
}

// POST /devices/{id}/send-image
// Body: {"to": "+628xxx", "image_base64": "...", "caption": "...", "mimetype": "image/jpeg"}
func (h *Handlers) sendImageMessage(w http.ResponseWriter, r *http.Request) {
	deviceID := r.PathValue("id")
	if deviceID == "" {
		writeError(w, http.StatusBadRequest, "device id required")
		return
	}

	var req struct {
		To          string `json:"to"`
		ImageBase64 string `json:"image_base64"`
		Caption     string `json:"caption"`
		Mimetype    string `json:"mimetype"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.To == "" || req.ImageBase64 == "" {
		writeError(w, http.StatusBadRequest, "to and image_base64 required")
		return
	}

	imageData, err := base64.StdEncoding.DecodeString(req.ImageBase64)
	if err != nil {
		writeError(w, http.StatusBadRequest, fmt.Sprintf("invalid base64: %v", err))
		return
	}

	log.Printf("[%s] send-image request → %s (%d bytes, caption: %q)", deviceID, req.To, len(imageData), req.Caption)

	if err := h.dm.SendImage(deviceID, req.To, imageData, req.Caption, req.Mimetype); err != nil {
		log.Printf("[%s] send-image FAILED → %s: %v", deviceID, req.To, err)
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}

	log.Printf("[%s] send-image OK → %s", deviceID, req.To)
	writeJSON(w, http.StatusOK, map[string]string{"status": "sent"})
}

// POST /devices/{id}/send-document
// Body: {"to": "+628xxx", "document_base64": "...", "filename": "report.pdf", "caption": "...", "mimetype": "application/pdf"}
func (h *Handlers) sendDocumentMessage(w http.ResponseWriter, r *http.Request) {
	deviceID := r.PathValue("id")
	if deviceID == "" {
		writeError(w, http.StatusBadRequest, "device id required")
		return
	}

	var req struct {
		To             string `json:"to"`
		DocumentBase64 string `json:"document_base64"`
		Filename       string `json:"filename"`
		Caption        string `json:"caption"`
		Mimetype       string `json:"mimetype"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.To == "" || req.DocumentBase64 == "" {
		writeError(w, http.StatusBadRequest, "to and document_base64 required")
		return
	}

	docData, err := base64.StdEncoding.DecodeString(req.DocumentBase64)
	if err != nil {
		writeError(w, http.StatusBadRequest, fmt.Sprintf("invalid base64: %v", err))
		return
	}

	log.Printf("[%s] send-document request → %s (%d bytes, filename: %q)", deviceID, req.To, len(docData), req.Filename)

	if err := h.dm.SendDocument(deviceID, req.To, docData, req.Filename, req.Caption, req.Mimetype); err != nil {
		log.Printf("[%s] send-document FAILED → %s: %v", deviceID, req.To, err)
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}

	log.Printf("[%s] send-document OK → %s", deviceID, req.To)
	writeJSON(w, http.StatusOK, map[string]string{"status": "sent"})
}

// DELETE /devices/{id}
func (h *Handlers) deleteDevice(w http.ResponseWriter, r *http.Request) {
	deviceID := r.PathValue("id")
	if deviceID == "" {
		writeError(w, http.StatusBadRequest, "device id required")
		return
	}

	if err := h.dm.Disconnect(deviceID); err != nil {
		writeError(w, http.StatusNotFound, err.Error())
		return
	}

	w.WriteHeader(http.StatusNoContent)
}
