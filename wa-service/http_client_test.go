package main

import (
	"io"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"go.mau.fi/whatsmeow/types"
)

type roundTripperFunc func(*http.Request) (*http.Response, error)

func (fn roundTripperFunc) RoundTrip(req *http.Request) (*http.Response, error) {
	return fn(req)
}

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

func TestWebhookPathLimitsOneHundredConcurrentMessages(t *testing.T) {
	const total = 100
	const limit = 48

	block := make(chan struct{})
	started := make(chan struct{}, total)
	var current int32
	var maximum int32
	client := &http.Client{Transport: roundTripperFunc(func(*http.Request) (*http.Response, error) {
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
		return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader(`{}`)), Header: make(http.Header)}, nil
	})}
	dm := &DeviceManager{
		pythonWebhook: "http://python.invalid/v1/channels/wa/incoming",
		httpClient:    client,
		webhookSlots:  make(chan struct{}, limit),
		devices:       make(map[string]*DeviceInfo),
	}

	var wg sync.WaitGroup
	for i := 0; i < total; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			dm.forwardWebhook([]byte(`{}`), "qa-device", "qa-user", "qa-chat", types.JID{})
		}()
	}
	for i := 0; i < limit; i++ {
		select {
		case <-started:
		case <-time.After(2 * time.Second):
			t.Fatal("webhook workers did not reach the configured concurrency")
		}
	}
	if got := atomic.LoadInt32(&maximum); got != limit {
		t.Fatalf("expected %d in-flight webhooks, got %d", limit, got)
	}
	close(block)
	finished := make(chan struct{})
	go func() { wg.Wait(); close(finished) }()
	select {
	case <-finished:
	case <-time.After(5 * time.Second):
		t.Fatal("all queued webhook workers should finish")
	}
	if got := atomic.LoadInt32(&maximum); got > limit {
		t.Fatalf("webhook concurrency exceeded limit: %d > %d", got, limit)
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
