package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"strings"
	"time"
	"unicode"
)

type Router struct {
	mainAPIURL  string
	mainAPIKey  string
	webhookURL  string
	autoAgentID string // if set, all messages auto-route to this agent (test mode)
	store       *ConnectionStore
	wa          *WhatsAppClient
}

func NewRouter(mainAPIURL, mainAPIKey string, store *ConnectionStore, webhookURL, autoAgentID string) *Router {
	if autoAgentID != "" {
		log.Printf("[dev-router] TEST MODE: all messages auto-routed to agent %s", autoAgentID)
	}
	return &Router{
		mainAPIURL:  mainAPIURL,
		mainAPIKey:  mainAPIKey,
		webhookURL:  webhookURL,
		autoAgentID: autoAgentID,
		store:       store,
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

func normalizeTrialCode(text string) string {
	var b strings.Builder
	for _, r := range strings.ToUpper(strings.TrimSpace(text)) {
		if unicode.IsLetter(r) || unicode.IsDigit(r) {
			b.WriteRune(r)
		}
	}
	code := b.String()
	if len(code) > 6 {
		return code[:6]
	}
	return code
}

func trialCodeCandidates(text string) []string {
	seen := map[string]bool{}
	candidates := []string{}
	for _, token := range strings.Fields(text) {
		code := normalizeTrialCode(token)
		if len(code) == 6 && strings.IndexFunc(code, unicode.IsDigit) >= 0 && !seen[code] {
			seen[code] = true
			candidates = append(candidates, code)
		}
	}
	code := normalizeTrialCode(text)
	if len(code) == 6 && strings.IndexFunc(code, unicode.IsDigit) >= 0 && !seen[code] {
		candidates = append(candidates, code)
	}
	return candidates
}

func messageConnectionKeys(msg IncomingMessage) []string {
	seen := map[string]bool{}
	keys := []string{}
	add := func(key string) {
		key = strings.TrimSpace(key)
		if key == "" || seen[key] {
			return
		}
		seen[key] = true
		keys = append(keys, key)
	}

	add(msg.From)
	add(msg.PhoneFrom)
	add(msg.ChatID)
	if user, _, ok := strings.Cut(msg.ChatID, "@"); ok && user != "" {
		add("+" + user)
	}
	return keys
}

func (r *Router) HandleMessage(msg IncomingMessage) {
	// Forward raw message to optional webhook regardless of routing
	if r.webhookURL != "" {
		go r.forwardWebhook(msg)
	}

	// TEST MODE: auto-route semua pesan ke agent tertentu
	if r.autoAgentID != "" {
		r.forwardToAgent(r.autoAgentID, msg)
		return
	}

	keys := messageConnectionKeys(msg)
	conn, connectedKey, connected := r.store.GetAny(keys...)

	if connected && isDisconnect(msg.Text) {
		_ = r.store.DeleteMany(keys...)
		_, _ = r.wa.SendText(msg.ChatID, "✅ Kamu berhasil disconnect dari agent.\n\nKirim kode baru dari Arthur kapan saja untuk connect ke agent lagi.")
		log.Printf("[dev-router] %s disconnected from agent %s", connectedKey, conn.AgentID)
		return
	}

	if connected {
		for _, code := range trialCodeCandidates(msg.Text) {
			if agentID, agentName, ok := r.claimTrialCode(code, msg.From, msg.PhoneFrom, msg.ChatID, msg.PushName); ok {
				r.saveConnection(keys, msg.ChatID, agentID)
				_, _ = r.wa.SendText(msg.ChatID, fmt.Sprintf("✅ Berhasil switch ke agent *%s*.\n\nSekarang kamu bisa chat langsung di sini.\nKirim */stop* kalau mau disconnect.", agentName))
				log.Printf("[dev-router] %s switched from agent %s to agent %s via trial code", connectedKey, conn.AgentID, agentID)
				return
			}
		}
	}

	if !connected {
		lower := strings.ToLower(strings.TrimSpace(msg.Text))
		if strings.HasPrefix(lower, "connect ") {
			agentID := strings.TrimSpace(msg.Text[len("connect "):])
			r.handleConnect(msg.From, msg.ChatID, agentID)
			return
		}
		codes := trialCodeCandidates(msg.Text)
		if len(codes) > 0 {
			for _, code := range codes {
				if agentID, agentName, ok := r.claimTrialCode(code, msg.From, msg.PhoneFrom, msg.ChatID, msg.PushName); ok {
					r.saveConnection(keys, msg.ChatID, agentID)
					_, _ = r.wa.SendText(msg.ChatID, fmt.Sprintf("✅ Berhasil terhubung ke agent *%s*!\n\nSekarang kamu bisa chat langsung di sini.\nKirim */stop* kalau mau disconnect.", agentName))
					log.Printf("[dev-router] %s connected to agent %s via trial code", msg.From, agentID)
					return
				}
			}
			if len(codes) == 1 {
				_, _ = r.wa.SendText(msg.ChatID, "❌ Kode tidak ditemukan atau sudah tidak aktif.\n\nMinta kode baru dari Arthur, lalu kirim 6 karakter kodenya ke sini.")
				return
			}
			_, _ = r.wa.SendText(msg.ChatID, "❌ Kode di pesan ini tidak ditemukan atau sudah tidak aktif.\n\nBuka link dari Arthur lagi, atau kirim 6 karakter kode saja.")
			return
		}
		// Check if this phone is an operator for any agent — auto-route without requiring 'connect'
		if agentID, ok := r.lookupOperatorAgent(msg.From, msg.PhoneFrom); ok {
			log.Printf("[dev-router] operator %s auto-routed to agent %s", msg.From, agentID)
			r.forwardToAgent(agentID, msg)
			return
		}
		// _ = r.wa.SendText(msg.ChatID, "👋 Halo! Ini adalah *WhatsApp Development Agent*.\n\nKirim perintah berikut untuk mulai:\n*connect AGENT_ID*\n\nContoh: connect abc123-def456\n\nDapatkan Agent ID dari dashboard.")
		return
	}

	// Forward to Python via /v1/channels/wa/incoming with virtual device_id
	r.forwardToAgent(conn.AgentID, msg)
}

func (r *Router) saveConnection(keys []string, chatID, agentID string) {
	conn := &UserConnection{
		AgentID:     agentID,
		ConnectedAt: time.Now(),
		ChatID:      chatID,
	}
	if err := r.store.SetMany(keys, conn); err != nil {
		log.Printf("[dev-router] store set err: %v", err)
	}
}

func (r *Router) handleConnect(from, chatID, agentID string) {
	_, _ = r.wa.SendText(chatID, "⏳ Menghubungkan ke agent...")

	agentName, err := r.fetchAgentName(agentID)
	if err != nil {
		log.Printf("[dev-router] fetch agent %s err: %v", agentID, err)
		_, _ = r.wa.SendText(chatID, fmt.Sprintf("❌ Agent ID *%s* tidak ditemukan. Pastikan Agent ID benar dan coba lagi.", agentID))
		return
	}

	r.saveConnection([]string{from, chatID}, chatID, agentID)

	_, _ = r.wa.SendText(chatID, fmt.Sprintf("✅ Berhasil terhubung ke agent *%s*!\n\nSekarang kamu bisa chat langsung di sini.\nKirim *berhenti* atau */stop* untuk disconnect.", agentName))
	log.Printf("[dev-router] %s connected to agent %s", from, agentID)
}

func (r *Router) claimTrialCode(codeText, from, phoneFrom, chatID, pushName string) (string, string, bool) {
	claimPhone := from
	if strings.TrimSpace(phoneFrom) != "" {
		claimPhone = phoneFrom
	}
	payload := map[string]string{
		"code":      normalizeTrialCode(codeText),
		"phone":     claimPhone,
		"chat_id":   chatID,
		"push_name": pushName,
	}
	data, _ := json.Marshal(payload)
	req, err := http.NewRequest("POST", r.mainAPIURL+"/v1/channels/wa-dev/claim-code", bytes.NewReader(data))
	if err != nil {
		log.Printf("[dev-router] build claim-code request err: %v", err)
		return "", "", false
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-API-Key", r.mainAPIKey)

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		log.Printf("[dev-router] claim-code request err: %v", err)
		return "", "", false
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		log.Printf("[dev-router] claim-code HTTP %d: %s", resp.StatusCode, string(b))
		return "", "", false
	}

	var result struct {
		AgentID   string `json:"agent_id"`
		AgentName string `json:"agent_name"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		log.Printf("[dev-router] decode claim-code err: %v", err)
		return "", "", false
	}
	return result.AgentID, result.AgentName, result.AgentID != ""
}

// forwardToAgent POSTs the message to Python /v1/channels/wa/incoming using a
// virtual device_id of the form "wadev_{agentID}". Python handles session
// management, media processing, escalation, and reminder delivery.
func (r *Router) forwardToAgent(agentID string, msg IncomingMessage) {
	deviceID := "wadev_" + agentID
	payload := map[string]interface{}{
		"device_id":          deviceID,
		"from":               msg.From,
		"phone_from":         msg.PhoneFrom,
		"chat_id":            msg.ChatID,
		"sender_alt":         msg.SenderAlt,
		"addressing_mode":    msg.AddressingMode,
		"message":            msg.Text,
		"message_id":         msg.MessageID,
		"timestamp":          msg.Timestamp,
		"push_name":          msg.PushName,
		"media_type":         msg.MediaType,
		"media_data":         msg.MediaData,
		"media_filename":     msg.MediaFilename,
		"quoted_text":        msg.QuotedText,
		"quoted_stanza_id":   msg.QuotedStanzaID,
		"quoted_participant": msg.QuotedParticipant,
		"quoted_remote_jid":  msg.QuotedRemoteJID,
	}

	data, _ := json.Marshal(payload)
	url := r.mainAPIURL + "/v1/channels/wa/incoming"
	req, err := http.NewRequest("POST", url, bytes.NewReader(data))
	if err != nil {
		log.Printf("[dev-router] build request err: %v", err)
		_, _ = r.wa.SendText(msg.ChatID, "❌ Error internal. Coba lagi.")
		return
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-API-Key", r.mainAPIKey)

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		log.Printf("[dev-router] forward to python err: %v", err)
		_, _ = r.wa.SendText(msg.ChatID, "❌ Terjadi error saat menghubungi agent. Coba lagi nanti.")
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		b, _ := io.ReadAll(resp.Body)
		log.Printf("[dev-router] python returned HTTP %d: %s", resp.StatusCode, string(b))
		_, _ = r.wa.SendText(msg.ChatID, "❌ Terjadi error saat menghubungi agent. Coba lagi nanti.")
		return
	}

	var result struct {
		Status string `json:"status"`
		Reply  string `json:"reply"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err == nil && result.Status == "send_failed" && strings.TrimSpace(result.Reply) != "" {
		if _, sendErr := r.wa.SendText(msg.ChatID, result.Reply); sendErr != nil {
			log.Printf("[dev-router] fallback send failed for %s: %v", msg.From, sendErr)
		} else {
			log.Printf("[dev-router] fallback sent python reply to %s after send_failed", msg.From)
		}
	}

	// Python normally sends the reply back via wa-dev-service's /send/text endpoint.
	// The fallback above only fires when Python explicitly reports that final delivery failed.
	log.Printf("[dev-router] forwarded msg from %s to agent %s", msg.From, agentID)
}

// lookupOperatorAgent checks whether a phone number is an operator for any agent.
// Used to auto-route escalation replies without requiring the operator to 'connect {agentID}'.
func (r *Router) lookupOperatorAgent(phones ...string) (string, bool) {
	for _, phone := range phones {
		if strings.TrimSpace(phone) == "" {
			continue
		}
		agentID, ok := r.lookupOperatorAgentOne(phone)
		if ok {
			return agentID, true
		}
	}
	return "", false
}

func (r *Router) lookupOperatorAgentOne(phone string) (string, bool) {
	url := fmt.Sprintf("%s/v1/channels/wa-dev/operator-route?phone=%s", r.mainAPIURL, url.QueryEscape(phone))
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
		"from":               msg.From,
		"chat_id":            msg.ChatID,
		"text":               msg.Text,
		"message_id":         msg.MessageID,
		"media_type":         msg.MediaType,
		"media_data":         msg.MediaData,
		"media_filename":     msg.MediaFilename,
		"media_mimetype":     msg.MediaMimetype,
		"quoted_text":        msg.QuotedText,
		"quoted_stanza_id":   msg.QuotedStanzaID,
		"quoted_participant": msg.QuotedParticipant,
		"quoted_remote_jid":  msg.QuotedRemoteJID,
		"timestamp":          msg.Timestamp,
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
