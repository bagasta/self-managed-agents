package main

import (
	"bytes"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestNormalizeTestPhone(t *testing.T) {
	got := normalizeTestPhone("+62 812-3456-7890@s.whatsapp.net")
	if got != "6281234567890" {
		t.Fatalf("unexpected normalized phone: %s", got)
	}
}

func TestTestModeBlocksEveryOtherOutboundTarget(t *testing.T) {
	dm := &DeviceManager{
		devices: make(map[string]*DeviceInfo),
		testConfig: TestModeConfig{
			Enabled:     true,
			TargetPhone: "628111111111",
		},
		testInbox: newTestInbox(TestModeConfig{
			Enabled:     true,
			TargetPhone: "628111111111",
		}),
	}

	_, err := dm.SendMessage("watest_arthur", "628222222222", "halo")
	if !errors.Is(err, ErrTestTargetBlocked) {
		t.Fatalf("expected target block, got %v", err)
	}

	_, err = dm.SendMessage("watest_arthur", "+628111111111", "halo")
	if errors.Is(err, ErrTestTargetBlocked) {
		t.Fatalf("configured target should be allowed: %v", err)
	}
}

func TestTestInboxAcceptsBoundLIDAndIgnoresOtherChats(t *testing.T) {
	inbox := newTestInbox(TestModeConfig{
		Enabled:     true,
		TargetPhone: "628111111111",
		MaxMessages: 10,
	})
	inbox.bindTargetJID("watest_arthur", "777777@lid")

	if !inbox.addIfTarget(TestMessage{
		DeviceID: "watest_arthur",
		From:     "+777777",
		ChatID:   "777777@lid",
		Message:  "Balasan Arthur",
	}) {
		t.Fatal("bound Arthur LID should be captured")
	}
	if inbox.addIfTarget(TestMessage{
		DeviceID:  "watest_arthur",
		PhoneFrom: "+628999999999",
		ChatID:    "628999999999@s.whatsapp.net",
		Message:   "Chat lain",
	}) {
		t.Fatal("non-target chat must be ignored")
	}

	messages := inbox.list("watest_arthur", 0)
	if len(messages) != 1 || messages[0].Message != "Balasan Arthur" {
		t.Fatalf("unexpected inbox: %#v", messages)
	}
	if messages[0].Seq != 1 || messages[0].ReceivedAt == "" {
		t.Fatalf("missing sequence metadata: %#v", messages[0])
	}
}

func TestTestModeAuthProtectsControlAndDeviceEndpoints(t *testing.T) {
	cfg := TestModeConfig{Enabled: true, APIKey: "0123456789abcdef"}
	handler := testModeAuth(cfg, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))

	unauthorized := httptest.NewRecorder()
	handler.ServeHTTP(unauthorized, httptest.NewRequest(http.MethodGet, "/test/messages", nil))
	if unauthorized.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d", unauthorized.Code)
	}

	authorizedRequest := httptest.NewRequest(http.MethodGet, "/devices/test/status", nil)
	authorizedRequest.Header.Set("X-Test-Key", cfg.APIKey)
	authorized := httptest.NewRecorder()
	handler.ServeHTTP(authorized, authorizedRequest)
	if authorized.Code != http.StatusNoContent {
		t.Fatalf("expected protected request to pass, got %d", authorized.Code)
	}

	health := httptest.NewRecorder()
	handler.ServeHTTP(health, httptest.NewRequest(http.MethodGet, "/health", nil))
	if health.Code != http.StatusNoContent {
		t.Fatalf("health must remain available to container checks, got %d", health.Code)
	}
}

func TestSendHandlerReturnsForbiddenForNonArthurTarget(t *testing.T) {
	cfg := TestModeConfig{
		Enabled:     true,
		TargetPhone: "628111111111",
	}
	handler := NewHandlers(&DeviceManager{
		devices:    make(map[string]*DeviceInfo),
		testConfig: cfg,
		testInbox:  newTestInbox(cfg),
	})
	request := httptest.NewRequest(
		http.MethodPost,
		"/devices/watest_arthur/send",
		bytes.NewBufferString(`{"to":"628222222222","message":"halo"}`),
	)
	request.SetPathValue("id", "watest_arthur")
	response := httptest.NewRecorder()

	handler.sendMessage(response, request)

	if response.Code != http.StatusForbidden {
		t.Fatalf("expected 403, got %d: %s", response.Code, response.Body.String())
	}
}
