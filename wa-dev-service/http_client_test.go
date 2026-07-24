package main

import (
	"io"
	"net/http"
	"strings"
	"testing"
	"time"
)

type roundTripperFunc func(*http.Request) (*http.Response, error)

func (fn roundTripperFunc) RoundTrip(req *http.Request) (*http.Response, error) {
	return fn(req)
}

func TestRouterUsesSharedBoundedHTTPTransport(t *testing.T) {
	t.Setenv("AGENT_MAX_IN_FLIGHT", "")
	router := NewRouter("http://localhost:8000", "test-key", nil, "", "")
	defer router.Close()

	if router.agentClient.Transport != router.transport || router.fastClient.Transport != router.transport {
		t.Fatal("router clients must share one reusable transport")
	}
	if router.webhookClient.Transport != router.transport {
		t.Fatal("webhook client must share the reusable transport")
	}
	if router.transport.MaxConnsPerHost != 128 {
		t.Fatalf("unexpected MaxConnsPerHost: %d", router.transport.MaxConnsPerHost)
	}
	if router.agentClient.Timeout != 330*time.Second {
		t.Fatalf("unexpected agent timeout: %s", router.agentClient.Timeout)
	}
	if router.fastClient.Timeout != 5*time.Second {
		t.Fatalf("unexpected fast client timeout: %s", router.fastClient.Timeout)
	}
	if cap(router.agentSlots) != 48 {
		t.Fatalf("unexpected agent slot capacity: %d", cap(router.agentSlots))
	}
}

func TestForwardToAgentUsesConfiguredClient(t *testing.T) {
	t.Setenv("AGENT_MAX_IN_FLIGHT", "2")
	router := NewRouter("http://main-api.invalid", "test-key", nil, "", "")
	defer router.Close()

	called := false
	router.agentClient = &http.Client{
		Transport: roundTripperFunc(func(req *http.Request) (*http.Response, error) {
			called = true
			return &http.Response{
				StatusCode: http.StatusOK,
				Body:       io.NopCloser(strings.NewReader(`{"status":"ok"}`)),
				Header:     make(http.Header),
			}, nil
		}),
		Timeout: time.Second,
	}

	router.forwardToAgent("agent-1", IncomingMessage{ChatID: "628111@s.whatsapp.net"})

	if !called {
		t.Fatal("forwardToAgent did not use router.agentClient")
	}
	if cap(router.agentSlots) != 2 {
		t.Fatalf("unexpected configured agent slot capacity: %d", cap(router.agentSlots))
	}
}

func TestMediaDownloadClientIsBounded(t *testing.T) {
	client := newMediaDownloadClient()
	transport, ok := client.Transport.(*http.Transport)
	if !ok {
		t.Fatalf("expected *http.Transport, got %T", client.Transport)
	}
	if client.Timeout != 60*time.Second {
		t.Fatalf("unexpected download timeout: %s", client.Timeout)
	}
	if transport.MaxConnsPerHost != 16 {
		t.Fatalf("unexpected download MaxConnsPerHost: %d", transport.MaxConnsPerHost)
	}
}
