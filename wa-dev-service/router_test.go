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
