package main

import (
	"net/http"
	"testing"
	"time"
)

func TestPythonWebhookClientIsBoundedAndReusable(t *testing.T) {
	client := newPythonWebhookClient()
	transport, ok := client.Transport.(*http.Transport)
	if !ok {
		t.Fatalf("expected *http.Transport, got %T", client.Transport)
	}
	if client.Timeout != 330*time.Second {
		t.Fatalf("unexpected timeout: %s", client.Timeout)
	}
	if transport.MaxConnsPerHost != 128 {
		t.Fatalf("unexpected MaxConnsPerHost: %d", transport.MaxConnsPerHost)
	}
	if transport.MaxIdleConnsPerHost != 64 {
		t.Fatalf("unexpected MaxIdleConnsPerHost: %d", transport.MaxIdleConnsPerHost)
	}
}

func TestWebhookMaxInFlightUsesSafeDefaultAndEnvironmentOverride(t *testing.T) {
	t.Setenv("WEBHOOK_MAX_IN_FLIGHT", "")
	if got := webhookMaxInFlight(); got != 48 {
		t.Fatalf("unexpected default webhook capacity: %d", got)
	}

	t.Setenv("WEBHOOK_MAX_IN_FLIGHT", "17")
	if got := webhookMaxInFlight(); got != 17 {
		t.Fatalf("unexpected configured webhook capacity: %d", got)
	}
}
