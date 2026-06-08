package main

import (
	"encoding/json"
	"os"
	"sync"
	"time"
)

type UserConnection struct {
	AgentID        string     `json:"agent_id"`
	SessionID      string     `json:"session_id"`
	ConnectedAt    time.Time  `json:"connected_at"`
	ChatID         string     `json:"chat_id"`
	Disconnected   bool       `json:"disconnected,omitempty"`
	DisconnectedAt *time.Time `json:"disconnected_at,omitempty"`
}

type ConnectionStore struct {
	mu          sync.RWMutex
	connections map[string]*UserConnection
	filePath    string
}

func NewConnectionStore(filePath string) (*ConnectionStore, error) {
	s := &ConnectionStore{
		connections: make(map[string]*UserConnection),
		filePath:    filePath,
	}
	if err := s.load(); err != nil {
		return nil, err
	}
	return s, nil
}

func (s *ConnectionStore) Get(phone string) (*UserConnection, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	c, ok := s.connections[phone]
	return c, ok
}

func (s *ConnectionStore) GetAny(keys ...string) (*UserConnection, string, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	for _, key := range keys {
		if key == "" {
			continue
		}
		if c, ok := s.connections[key]; ok {
			return c, key, true
		}
	}
	return nil, "", false
}

func (s *ConnectionStore) Set(phone string, conn *UserConnection) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.connections[phone] = conn
	return s.save()
}

func (s *ConnectionStore) SetMany(keys []string, conn *UserConnection) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, key := range keys {
		if key != "" {
			s.connections[key] = conn
		}
	}
	return s.save()
}

func (s *ConnectionStore) SuppressMany(keys []string, chatID, agentID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	targetKeys := map[string]bool{}
	for _, key := range keys {
		if key != "" {
			targetKeys[key] = true
		}
	}
	for key, conn := range s.connections {
		if conn == nil {
			continue
		}
		if conn.AgentID == agentID && (chatID == "" || conn.ChatID == "" || conn.ChatID == chatID) {
			targetKeys[key] = true
		}
	}

	now := time.Now()
	for key := range targetKeys {
		s.connections[key] = &UserConnection{
			AgentID:        agentID,
			ConnectedAt:    now,
			ChatID:         chatID,
			Disconnected:   true,
			DisconnectedAt: &now,
		}
	}
	return s.save()
}

func (s *ConnectionStore) Delete(phone string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.connections, phone)
	return s.save()
}

func (s *ConnectionStore) DeleteMany(keys ...string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, key := range keys {
		if key != "" {
			delete(s.connections, key)
		}
	}
	return s.save()
}

func (s *ConnectionStore) List() map[string]*UserConnection {
	s.mu.RLock()
	defer s.mu.RUnlock()
	result := make(map[string]*UserConnection, len(s.connections))
	for k, v := range s.connections {
		result[k] = v
	}
	return result
}

func (s *ConnectionStore) load() error {
	data, err := os.ReadFile(s.filePath)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	return json.Unmarshal(data, &s.connections)
}

func (s *ConnectionStore) save() error {
	data, err := json.MarshalIndent(s.connections, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(s.filePath, data, 0644)
}
