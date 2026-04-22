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
		} else {
			_ = r.wa.SendText(msg.ChatID, "👋 Halo! Ini adalah *WhatsApp Development Agent*.\n\nKirim perintah berikut untuk mulai:\n*connect AGENT_ID*\n\nContoh: connect abc123-def456\n\nDapatkan Agent ID dari dashboard.")
		}
		return
	}

	// Forward to agent — build text with media context if any
	agentText := msg.Text
	if msg.MediaType != "" && msg.MediaData != "" {
		agentText = fmt.Sprintf("[%s: %s]\n%s", msg.MediaType, msg.MediaFilename, msg.Text)
	}

	reply, err := r.callAgentAPI(conn.AgentID, conn.AgentKey, conn.SessionID, agentText)
	if err != nil {
		log.Printf("[dev-router] agent API error for %s: %v", msg.From, err)
		_ = r.wa.SendText(msg.ChatID, "❌ Terjadi error saat menghubungi agent. Coba lagi nanti.")
		return
	}

	if reply != "" {
		_ = r.wa.SendText(msg.ChatID, reply)
	}
}

func (r *Router) handleConnect(from, chatID, agentID string) {
	_ = r.wa.SendText(chatID, "⏳ Menghubungkan ke agent...")

	agentKey, agentName, err := r.fetchAgentKey(agentID)
	if err != nil {
		log.Printf("[dev-router] fetch agent %s err: %v", agentID, err)
		_ = r.wa.SendText(chatID, fmt.Sprintf("❌ Agent ID *%s* tidak ditemukan. Pastikan Agent ID benar dan coba lagi.", agentID))
		return
	}

	sessionID, err := r.createSession(agentID, agentKey, from)
	if err != nil {
		log.Printf("[dev-router] create session for agent %s err: %v", agentID, err)
		_ = r.wa.SendText(chatID, "❌ Gagal membuat sesi. Coba lagi.")
		return
	}

	conn := &UserConnection{
		AgentID:     agentID,
		AgentKey:    agentKey,
		SessionID:   sessionID,
		ConnectedAt: time.Now(),
		ChatID:      chatID,
	}
	if err := r.store.Set(from, conn); err != nil {
		log.Printf("[dev-router] store set err: %v", err)
	}

	_ = r.wa.SendText(chatID, fmt.Sprintf("✅ Berhasil terhubung ke agent *%s*!\n\nSekarang kamu bisa chat langsung di sini.\nKirim *berhenti* atau */stop* untuk disconnect.", agentName))
	log.Printf("[dev-router] %s connected to agent %s (session: %s)", from, agentID, sessionID)
}

func (r *Router) fetchAgentKey(agentID string) (apiKey, name string, err error) {
	req, err := http.NewRequest("GET", fmt.Sprintf("%s/v1/agents/%s", r.mainAPIURL, agentID), nil)
	if err != nil {
		return "", "", err
	}
	req.Header.Set("X-API-Key", r.mainAPIKey)

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return "", "", err
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		b, _ := io.ReadAll(resp.Body)
		return "", "", fmt.Errorf("HTTP %d: %s", resp.StatusCode, string(b))
	}

	var result struct {
		APIKey string `json:"api_key"`
		Name   string `json:"name"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return "", "", err
	}
	if result.APIKey == "" {
		return "", "", fmt.Errorf("empty api_key in agent response")
	}
	return result.APIKey, result.Name, nil
}

func (r *Router) createSession(agentID, agentKey, externalUserID string) (string, error) {
	body, _ := json.Marshal(map[string]string{"external_user_id": externalUserID})
	req, err := http.NewRequest("POST", fmt.Sprintf("%s/v1/agents/%s/sessions", r.mainAPIURL, agentID), bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-API-Key", r.mainAPIKey)
	req.Header.Set("X-Agent-Key", agentKey)

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
		ID string `json:"id"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return "", err
	}
	if result.ID == "" {
		return "", fmt.Errorf("empty session ID in response")
	}
	return result.ID, nil
}

func (r *Router) callAgentAPI(agentID, agentKey, sessionID, message string) (string, error) {
	body, _ := json.Marshal(map[string]string{"message": message})
	url := fmt.Sprintf("%s/v1/agents/%s/sessions/%s/messages", r.mainAPIURL, agentID, sessionID)
	req, err := http.NewRequest("POST", url, bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-API-Key", r.mainAPIKey)
	req.Header.Set("X-Agent-Key", agentKey)

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
		Reply string `json:"reply"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return "", err
	}
	return result.Reply, nil
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
