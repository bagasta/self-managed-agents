package main

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type roundTripperFunc func(*http.Request) (*http.Response, error)

func (fn roundTripperFunc) RoundTrip(req *http.Request) (*http.Response, error) {
	return fn(req)
}

func TestAgentRequestTimeoutRecognizesWrappedDeadline(t *testing.T) {
	timeoutErr := &url.Error{
		Op:  "Post",
		URL: "http://api:8000/v1/channels/wa/incoming",
		Err: context.DeadlineExceeded,
	}
	if !isAgentRequestTimeout(timeoutErr) {
		t.Fatal("wrapped context deadline must be treated as an uncertain in-flight agent request")
	}
	if isAgentRequestTimeout(errors.New("connection refused")) {
		t.Fatal("connection refusal must remain a real forwarding failure")
	}
}

func TestAgentForwardingLimitsOneHundredConcurrentUsers(t *testing.T) {
	const total = 100
	const limit = 48
	t.Setenv("AGENT_MAX_IN_FLIGHT", "48")

	router := NewRouter("http://main-api.invalid", "test-key", nil, "", "")
	defer router.Close()
	block := make(chan struct{})
	started := make(chan struct{}, total)
	var current int32
	var maximum int32
	router.agentClient = &http.Client{Transport: roundTripperFunc(func(*http.Request) (*http.Response, error) {
		now := atomic.AddInt32(&current, 1)
		for {
			seen := atomic.LoadInt32(&maximum)
			if now <= seen || atomic.CompareAndSwapInt32(&maximum, seen, now) {
				break
			}
		}
		started <- struct{}{}
		<-block
		atomic.AddInt32(&current, -1)
		return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader(`{"status":"ok"}`)), Header: make(http.Header)}, nil
	})}

	var wg sync.WaitGroup
	for i := 0; i < total; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			router.forwardToAgent("qa-agent", IncomingMessage{From: "qa-user", ChatID: "qa-chat-" + string(rune(i))})
		}(i)
	}
	for i := 0; i < limit; i++ {
		select {
		case <-started:
		case <-time.After(2 * time.Second):
			t.Fatal("agent forwards did not reach the configured concurrency")
		}
	}
	if got := atomic.LoadInt32(&maximum); got != limit {
		t.Fatalf("expected %d in-flight agent forwards, got %d", limit, got)
	}
	close(block)
	finished := make(chan struct{})
	go func() { wg.Wait(); close(finished) }()
	select {
	case <-finished:
	case <-time.After(5 * time.Second):
		t.Fatal("all queued agent forwards should finish")
	}
	if got := atomic.LoadInt32(&maximum); got > limit {
		t.Fatalf("agent forwarding concurrency exceeded limit: %d > %d", got, limit)
	}
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
