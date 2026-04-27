package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
	"time"
)

type Router struct {
	mainAPIURL string
	mainAPIKey string
	webhookURL string
	store      *ConnectionStore
	wa         *WhatsAppClient
}

func NewRouter(mainAPIURL, mainAPIKey string, store *ConnectionStore, webhookURL string) *Router {
	return &Router{
		mainAPIURL: mainAPIURL,
		mainAPIKey: mainAPIKey,
		webhookURL: webhookURL,
		store:      store,
	}
}

func (r *Router) SetWA(wa *WhatsAppClient) {
	r.wa = wa
}

var disconnectKeywords = []string{"/stop", "berhenti", "/disconnect", "stop"}

func isDisconnect(text string) bool {
	lower := strings.ToLower(strings.TrimSpace(text))
	for _, kw := range disconnectKeywords {
		if lower == kw {
			return true
		}
	}
	return false
}

func (r *Router) HandleMessage(msg IncomingMessage) {
	// Forward raw message to optional webhook regardless of routing
	if r.webhookURL != "" {
		go r.forwardWebhook(msg)
	}

	conn, connected := r.store.Get(msg.From)

	if connected && isDisconnect(msg.Text) {
		_ = r.store.Delete(msg.From)
		_ = r.wa.SendText(msg.ChatID, "✅ Kamu berhasil disconnect dari agent.\n\nKirim *connect AGENT_ID* kapan saja untuk connect ke agent lagi.")
		log.Printf("[dev-router] %s disconnected from agent %s", msg.From, conn.AgentID)
		return
	}

	if !connected {
		lower := strings.ToLower(strings.TrimSpace(msg.Text))
		if strings.HasPrefix(lower, "connect ") {
			agentID := strings.TrimSpace(msg.Text[len("connect "):])
			r.handleConnect(msg.From, msg.ChatID, agentID)
			return
		}
		// Check if this phone is an operator for any agent — auto-route without requiring 'connect'
		if agentID, ok := r.lookupOperatorAgent(msg.From); ok {
			log.Printf("[dev-router] operator %s auto-routed to agent %s", msg.From, agentID)
			r.forwardToAgent(agentID, msg)
			return
		}
		_ = r.wa.SendText(msg.ChatID, "👋 Halo! Ini adalah *WhatsApp Development Agent*.\n\nKirim perintah berikut untuk mulai:\n*connect AGENT_ID*\n\nContoh: connect abc123-def456\n\nDapatkan Agent ID dari dashboard.")
		return
	}

	// Forward to Python via /v1/channels/wa/incoming with virtual device_id
	r.forwardToAgent(conn.AgentID, msg)
}

func (r *Router) handleConnect(from, chatID, agentID string) {
	_ = r.wa.SendText(chatID, "⏳ Menghubungkan ke agent...")

	agentName, err := r.fetchAgentName(agentID)
	if err != nil {
		log.Printf("[dev-router] fetch agent %s err: %v", agentID, err)
		_ = r.wa.SendText(chatID, fmt.Sprintf("❌ Agent ID *%s* tidak ditemukan. Pastikan Agent ID benar dan coba lagi.", agentID))
		return
	}

	conn := &UserConnection{
		AgentID:     agentID,
		ConnectedAt: time.Now(),
		ChatID:      chatID,
	}
	if err := r.store.Set(from, conn); err != nil {
		log.Printf("[dev-router] store set err: %v", err)
	}

	_ = r.wa.SendText(chatID, fmt.Sprintf("✅ Berhasil terhubung ke agent *%s*!\n\nSekarang kamu bisa chat langsung di sini.\nKirim *berhenti* atau */stop* untuk disconnect.", agentName))
	log.Printf("[dev-router] %s connected to agent %s", from, agentID)
}

// forwardToAgent POSTs the message to Python /v1/channels/wa/incoming using a
// virtual device_id of the form "wadev_{agentID}". Python handles session
// management, media processing, escalation, and reminder delivery.
func (r *Router) forwardToAgent(agentID string, msg IncomingMessage) {
	deviceID := "wadev_" + agentID
	payload := map[string]interface{}{
		"device_id":      deviceID,
		"from":           msg.From,
		"chat_id":        msg.ChatID,
		"message":        msg.Text,
		"timestamp":      msg.Timestamp,
		"push_name":      msg.PushName,
		"media_type":     msg.MediaType,
		"media_data":     msg.MediaData,
		"media_filename": msg.MediaFilename,
	}

	data, _ := json.Marshal(payload)
	url := r.mainAPIURL + "/v1/channels/wa/incoming"
	req, err := http.NewRequest("POST", url, bytes.NewReader(data))
	if err != nil {
		log.Printf("[dev-router] build request err: %v", err)
		_ = r.wa.SendText(msg.ChatID, "❌ Error internal. Coba lagi.")
		return
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-API-Key", r.mainAPIKey)

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		log.Printf("[dev-router] forward to python err: %v", err)
		_ = r.wa.SendText(msg.ChatID, "❌ Terjadi error saat menghubungi agent. Coba lagi nanti.")
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		b, _ := io.ReadAll(resp.Body)
		log.Printf("[dev-router] python returned HTTP %d: %s", resp.StatusCode, string(b))
		_ = r.wa.SendText(msg.ChatID, "❌ Terjadi error saat menghubungi agent. Coba lagi nanti.")
		return
	}

	// Python already sends the reply back to the user via wa-dev-service's /send/text endpoint.
	// Nothing more to do here.
	log.Printf("[dev-router] forwarded msg from %s to agent %s", msg.From, agentID)
}

// lookupOperatorAgent checks whether a phone number is an operator for any agent.
// Used to auto-route escalation replies without requiring the operator to 'connect {agentID}'.
func (r *Router) lookupOperatorAgent(phone string) (string, bool) {
	url := fmt.Sprintf("%s/v1/channels/wa-dev/operator-route?phone=%s", r.mainAPIURL, phone)
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return "", false
	}
	req.Header.Set("X-API-Key", r.mainAPIKey)

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return "", false
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return "", false
	}

	var result struct {
		AgentID string `json:"agent_id"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return "", false
	}
	return result.AgentID, result.AgentID != ""
}

func (r *Router) fetchAgentName(agentID string) (string, error) {
	req, err := http.NewRequest("GET", fmt.Sprintf("%s/v1/agents/%s", r.mainAPIURL, agentID), nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("X-API-Key", r.mainAPIKey)

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		b, _ := io.ReadAll(resp.Body)
		return "", fmt.Errorf("HTTP %d: %s", resp.StatusCode, string(b))
	}

	var result struct {
		Name string `json:"name"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return "", err
	}
	return result.Name, nil
}

func (r *Router) forwardWebhook(msg IncomingMessage) {
	payload := map[string]interface{}{
		"from":           msg.From,
		"chat_id":        msg.ChatID,
		"text":           msg.Text,
		"media_type":     msg.MediaType,
		"media_data":     msg.MediaData,
		"media_filename": msg.MediaFilename,
		"media_mimetype": msg.MediaMimetype,
		"timestamp":      msg.Timestamp,
	}
	data, _ := json.Marshal(payload)
	resp, err := http.Post(r.webhookURL, "application/json", bytes.NewReader(data))
	if err != nil {
		log.Printf("[dev-router] webhook forward err: %v", err)
		return
	}
	resp.Body.Close()
	if resp.StatusCode >= 400 {
		log.Printf("[dev-router] webhook returned HTTP %d", resp.StatusCode)
	}
}
