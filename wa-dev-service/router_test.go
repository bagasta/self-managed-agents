package main

import (
	"reflect"
	"testing"
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
