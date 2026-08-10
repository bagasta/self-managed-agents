package main

import (
	"crypto/subtle"
	"errors"
	"fmt"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode"
)

var ErrTestTargetBlocked = errors.New("test mode target blocked")

type TestModeConfig struct {
	Enabled     bool
	TargetPhone string
	APIKey      string
	MaxMessages int
}

func loadTestModeConfig() (TestModeConfig, error) {
	enabled, _ := strconv.ParseBool(strings.TrimSpace(os.Getenv("TEST_MODE")))
	cfg := TestModeConfig{
		Enabled:     enabled,
		TargetPhone: normalizeTestPhone(os.Getenv("TEST_TARGET_PHONE")),
		APIKey:      strings.TrimSpace(os.Getenv("TEST_API_KEY")),
		MaxMessages: 500,
	}
	if !cfg.Enabled {
		return cfg, nil
	}
	if len(cfg.TargetPhone) < 8 || len(cfg.TargetPhone) > 15 {
		return TestModeConfig{}, fmt.Errorf(
			"TEST_TARGET_PHONE must contain 8-15 digits in international format",
		)
	}
	if len(cfg.APIKey) < 16 {
		return TestModeConfig{}, fmt.Errorf("TEST_API_KEY must contain at least 16 characters")
	}
	return cfg, nil
}

func normalizeTestPhone(value string) string {
	value = strings.Split(strings.TrimSpace(value), "@")[0]
	var digits strings.Builder
	for _, char := range value {
		if unicode.IsDigit(char) {
			digits.WriteRune(char)
		}
	}
	return digits.String()
}

func (cfg TestModeConfig) allowsTarget(value string) bool {
	return !cfg.Enabled || normalizeTestPhone(value) == cfg.TargetPhone
}

func testModeAuth(cfg TestModeConfig, next http.Handler) http.Handler {
	if !cfg.Enabled {
		return next
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/health" {
			next.ServeHTTP(w, r)
			return
		}
		provided := r.Header.Get("X-Test-Key")
		if len(provided) != len(cfg.APIKey) ||
			subtle.ConstantTimeCompare([]byte(provided), []byte(cfg.APIKey)) != 1 {
			writeError(w, http.StatusUnauthorized, "invalid test api key")
			return
		}
		next.ServeHTTP(w, r)
	})
}

type TestMessage struct {
	Seq           uint64 `json:"seq"`
	DeviceID      string `json:"device_id"`
	From          string `json:"from"`
	PhoneFrom     string `json:"phone_from"`
	ChatID        string `json:"chat_id"`
	SenderAlt     string `json:"sender_alt,omitempty"`
	Message       string `json:"message"`
	MessageID     string `json:"message_id"`
	MediaType     string `json:"media_type,omitempty"`
	MediaFilename string `json:"media_filename,omitempty"`
	MediaMimetype string `json:"media_mimetype,omitempty"`
	Timestamp     int64  `json:"timestamp"`
	ReceivedAt    string `json:"received_at"`
}

type TestInbox struct {
	mu          sync.RWMutex
	nextSeq     uint64
	maxMessages int
	targetPhone string
	targetJIDs  map[string]string
	messages    []TestMessage
}

func newTestInbox(cfg TestModeConfig) *TestInbox {
	maxMessages := cfg.MaxMessages
	if maxMessages < 1 {
		maxMessages = 500
	}
	return &TestInbox{
		maxMessages: maxMessages,
		targetPhone: cfg.TargetPhone,
		targetJIDs:  make(map[string]string),
		messages:    make([]TestMessage, 0, maxMessages),
	}
}

func (inbox *TestInbox) bindTargetJID(deviceID, jid string) {
	if inbox == nil || strings.TrimSpace(deviceID) == "" || strings.TrimSpace(jid) == "" {
		return
	}
	inbox.mu.Lock()
	inbox.targetJIDs[deviceID] = strings.TrimSpace(jid)
	inbox.mu.Unlock()
}

func (inbox *TestInbox) isTarget(
	deviceID, from, phoneFrom, chatID, senderAlt string,
) bool {
	if inbox == nil {
		return false
	}
	inbox.mu.RLock()
	boundJID := inbox.targetJIDs[deviceID]
	inbox.mu.RUnlock()
	if boundJID != "" && (chatID == boundJID || from == boundJID || senderAlt == boundJID) {
		return true
	}
	for _, value := range []string{phoneFrom, from, chatID, senderAlt} {
		if normalizeTestPhone(value) == inbox.targetPhone {
			return true
		}
	}
	return false
}

func (inbox *TestInbox) addIfTarget(message TestMessage) bool {
	if !inbox.isTarget(
		message.DeviceID,
		message.From,
		message.PhoneFrom,
		message.ChatID,
		message.SenderAlt,
	) {
		return false
	}
	inbox.mu.Lock()
	defer inbox.mu.Unlock()
	inbox.nextSeq++
	message.Seq = inbox.nextSeq
	message.ReceivedAt = time.Now().UTC().Format(time.RFC3339Nano)
	inbox.messages = append(inbox.messages, message)
	if overflow := len(inbox.messages) - inbox.maxMessages; overflow > 0 {
		inbox.messages = append([]TestMessage(nil), inbox.messages[overflow:]...)
	}
	return true
}

func (inbox *TestInbox) list(deviceID string, afterSeq uint64) []TestMessage {
	if inbox == nil {
		return []TestMessage{}
	}
	inbox.mu.RLock()
	defer inbox.mu.RUnlock()
	result := make([]TestMessage, 0)
	for _, message := range inbox.messages {
		if message.Seq <= afterSeq || (deviceID != "" && message.DeviceID != deviceID) {
			continue
		}
		result = append(result, message)
	}
	return result
}

func (inbox *TestInbox) reset(deviceID string) {
	if inbox == nil {
		return
	}
	inbox.mu.Lock()
	defer inbox.mu.Unlock()
	if deviceID == "" {
		inbox.messages = nil
		return
	}
	kept := inbox.messages[:0]
	for _, message := range inbox.messages {
		if message.DeviceID != deviceID {
			kept = append(kept, message)
		}
	}
	inbox.messages = kept
}
