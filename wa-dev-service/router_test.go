package main

import (
	"path/filepath"
	"reflect"
	"testing"
	"time"
)

func TestTrialCodeCandidates(t *testing.T) {
	got := trialCodeCandidates("Halo Arthur, kode saya: AB12C3")
	want := []string{"AB12C3"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("trialCodeCandidates() = %#v, want %#v", got, want)
	}
}

func TestTrialCodeCandidatesExactCode(t *testing.T) {
	got := trialCodeCandidates("ab-12c3")
	want := []string{"AB12C3"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("trialCodeCandidates() = %#v, want %#v", got, want)
	}
}

func TestMessageConnectionKeysIncludesLIDPhoneAndChatAliases(t *testing.T) {
	got := messageConnectionKeys(IncomingMessage{
		From:      "+103160936972328",
		PhoneFrom: "+628123456789",
		ChatID:    "103160936972328@lid",
	})
	want := []string{
		"+103160936972328",
		"+628123456789",
		"103160936972328@lid",
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("messageConnectionKeys() = %#v, want %#v", got, want)
	}
}

func TestMessageConnectionKeysAddsPhoneAliasFromChatID(t *testing.T) {
	got := messageConnectionKeys(IncomingMessage{
		From:   "+628123456789",
		ChatID: "628123456789@s.whatsapp.net",
	})
	want := []string{
		"+628123456789",
		"628123456789@s.whatsapp.net",
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("messageConnectionKeys() = %#v, want %#v", got, want)
	}
}

func TestOperatorAutoRouteRequiresQuotedContext(t *testing.T) {
	if hasOperatorRouteContext(IncomingMessage{Text: "hi"}) {
		t.Fatal("plain direct message should not auto-route as operator")
	}
	if !hasOperatorRouteContext(IncomingMessage{Text: "ya", QuotedStanzaID: "abc123"}) {
		t.Fatal("quoted operator reply should be eligible for operator auto-route")
	}
}

func TestSuppressManyMarksAllAgentChatAliasesDisconnected(t *testing.T) {
	store, err := NewConnectionStore(filepath.Join(t.TempDir(), "connections.json"))
	if err != nil {
		t.Fatalf("NewConnectionStore() err = %v", err)
	}

	conn := &UserConnection{
		AgentID:     "agent-1",
		ConnectedAt: time.Now(),
		ChatID:      "628123456789@s.whatsapp.net",
	}
	if err := store.SetMany([]string{"+628123456789", "628123456789@s.whatsapp.net", "old-alias"}, conn); err != nil {
		t.Fatalf("SetMany() err = %v", err)
	}
	if err := store.SuppressMany([]string{"+628123456789"}, "628123456789@s.whatsapp.net", "agent-1"); err != nil {
		t.Fatalf("SuppressMany() err = %v", err)
	}

	for _, key := range []string{"+628123456789", "628123456789@s.whatsapp.net", "old-alias"} {
		got, ok := store.Get(key)
		if !ok {
			t.Fatalf("expected key %s to remain suppressed", key)
		}
		if !isDisconnectedConnection(got) {
			t.Fatalf("key %s was not marked disconnected: %#v", key, got)
		}
	}
}
