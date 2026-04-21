package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/skip2/go-qrcode"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"

	_ "github.com/mattn/go-sqlite3"
)

type DeviceStatus string

const (
	StatusWaitingQR    DeviceStatus = "waiting_qr"
	StatusConnected    DeviceStatus = "connected"
	StatusDisconnected DeviceStatus = "disconnected"
)

type DeviceInfo struct {
	Client      *whatsmeow.Client
	Container   *sqlstore.Container
	Status      DeviceStatus
	PhoneNumber string // set after successful QR scan
	LatestQR    string // base64 PNG of latest QR code
	QRUpdated   time.Time
}

type DeviceManager struct {
	mu            sync.RWMutex
	devices       map[string]*DeviceInfo
	pythonWebhook string
	storeDir      string
}

func NewDeviceManager(pythonWebhook, storeDir string) (*DeviceManager, error) {
	if err := os.MkdirAll(storeDir, 0755); err != nil {
		return nil, fmt.Errorf("mkdir store: %w", err)
	}
	dm := &DeviceManager{
		devices:       make(map[string]*DeviceInfo),
		pythonWebhook: pythonWebhook,
		storeDir:      storeDir,
	}
	dm.loadExistingDevices()
	return dm, nil
}

// CreateDevice initialises a new WhatsApp device. Returns base64 PNG QR string.
// If the device is already connected (persisted session), returns empty string.
func (dm *DeviceManager) CreateDevice(deviceID string) (string, error) {
	dm.mu.RLock()
	existing, ok := dm.devices[deviceID]
	dm.mu.RUnlock()

	if ok && existing.Status == StatusConnected {
		return "", nil // already connected, no QR needed
	}

	dbPath := fmt.Sprintf("file:%s/%s.db?_foreign_keys=on", dm.storeDir, deviceID)
	dbLog := waLog.Stdout("DB", "ERROR", true)
	container, err := sqlstore.New(context.Background(), "sqlite3", dbPath, dbLog)
	if err != nil {
		return "", fmt.Errorf("sqlstore: %w", err)
	}

	deviceStore, err := container.GetFirstDevice(context.Background())
	if err != nil {
		deviceStore = container.NewDevice()
	}

	clientLog := waLog.Stdout("WA", "ERROR", true)
	client := whatsmeow.NewClient(deviceStore, clientLog)

	info := &DeviceInfo{
		Client:    client,
		Container: container,
		Status:    StatusWaitingQR,
	}

	dm.mu.Lock()
	dm.devices[deviceID] = info
	dm.mu.Unlock()

	client.AddEventHandler(dm.makeEventHandler(deviceID))

	// Already registered — just reconnect, no QR needed
	if client.Store.ID != nil {
		if err = client.Connect(); err != nil {
			log.Printf("[%s] reconnect err: %v", deviceID, err)
		} else {
			dm.mu.Lock()
			info.Status = StatusConnected
			if client.Store.ID != nil {
				info.PhoneNumber = client.Store.ID.User
			}
			dm.mu.Unlock()
		}
		return "", nil
	}

	// New device — need QR scan
	qrChan, err := client.GetQRChannel(context.Background())
	if err != nil {
		return "", fmt.Errorf("GetQRChannel: %w", err)
	}

	if err = client.Connect(); err != nil {
		return "", fmt.Errorf("connect: %w", err)
	}

	// Block until first QR arrives
	firstQR := make(chan string, 1)
	go func() {
		for evt := range qrChan {
			switch evt.Event {
			case "code":
				png, genErr := qrcode.Encode(evt.Code, qrcode.Medium, 256)
				if genErr != nil {
					log.Printf("[%s] qr encode err: %v", deviceID, genErr)
					continue
				}
				b64 := base64.StdEncoding.EncodeToString(png)
				dm.mu.Lock()
				if di, exists := dm.devices[deviceID]; exists {
					di.LatestQR = b64
					di.QRUpdated = time.Now()
				}
				dm.mu.Unlock()
				select {
				case firstQR <- b64:
				default:
				}
			case "success":
				dm.mu.Lock()
				if di, exists := dm.devices[deviceID]; exists {
					di.Status = StatusConnected
					di.LatestQR = ""
					if di.Client.Store.ID != nil {
						di.PhoneNumber = di.Client.Store.ID.User
					}
				}
				dm.mu.Unlock()
			}
		}
	}()

	select {
	case qr := <-firstQR:
		return qr, nil
	case <-time.After(30 * time.Second):
		return "", fmt.Errorf("timeout waiting for QR code")
	}
}

// GetQR returns the latest cached QR PNG (base64). Empty string if connected.
func (dm *DeviceManager) GetQR(deviceID string) (string, DeviceStatus, error) {
	dm.mu.RLock()
	info, ok := dm.devices[deviceID]
	dm.mu.RUnlock()

	if !ok {
		return "", "", fmt.Errorf("device %s not found", deviceID)
	}
	return info.LatestQR, info.Status, nil
}

// GetStatus returns connection status and phone number.
func (dm *DeviceManager) GetStatus(deviceID string) (DeviceStatus, string, error) {
	dm.mu.RLock()
	info, ok := dm.devices[deviceID]
	dm.mu.RUnlock()

	if !ok {
		return "", "", fmt.Errorf("device %s not found", deviceID)
	}
	return info.Status, info.PhoneNumber, nil
}

// SendMessage sends a text message to a WhatsApp number.
func (dm *DeviceManager) SendMessage(deviceID, to, text string) error {
	dm.mu.RLock()
	info, ok := dm.devices[deviceID]
	dm.mu.RUnlock()

	if !ok {
		return fmt.Errorf("device %s not found", deviceID)
	}
	if info.Status != StatusConnected {
		return fmt.Errorf("device %s not connected (status: %s)", deviceID, info.Status)
	}

	// Verify the client has a valid device JID
	if info.Client.Store.ID == nil {
		// Try to reconnect
		log.Printf("[%s] store has no device JID, attempting reconnect...", deviceID)
		if !info.Client.IsConnected() {
			if err := info.Client.Connect(); err != nil {
				return fmt.Errorf("device %s: reconnect failed: %w", deviceID, err)
			}
			time.Sleep(2 * time.Second) // give it a moment to establish
		}
		if info.Client.Store.ID == nil {
			return fmt.Errorf("device %s: no valid WA session (needs re-scan QR)", deviceID)
		}
	}

	// Parse JID tujuan.
	// Jika `to` mengandung "@" (misal "phone@s.whatsapp.net", "phone@lid", "group@g.us"),
	// parse langsung — ini mempertahankan server yang benar (penting untuk LID accounts).
	// Fallback ke DefaultUserServer jika hanya nomor telepon yang diberikan.
	var jid types.JID
	if strings.Contains(to, "@") {
		parsed, err := types.ParseJID(to)
		if err != nil {
			return fmt.Errorf("invalid JID %q: %w", to, err)
		}
		// Tolak AD JID (device-specific JID seperti "phone:5@s.whatsapp.net") —
		// whatsmeow mengembalikan ErrRecipientADJID untuk ini.
		if parsed.Device > 0 {
			return fmt.Errorf("cannot send to AD JID %q — use non-device JID", to)
		}
		jid = parsed
	} else {
		phone := strings.TrimPrefix(to, "+")
		jid = types.NewJID(phone, types.DefaultUserServer)
	}

	msg := &waE2E.Message{
		Conversation: proto.String(text),
	}

	_, err := info.Client.SendMessage(context.Background(), jid, msg)
	if err != nil {
		log.Printf("[%s] send to %s failed: %v", deviceID, to, err)
	}
	return err
}

// Disconnect logs out and removes the device.
func (dm *DeviceManager) Disconnect(deviceID string) error {
	dm.mu.Lock()
	info, ok := dm.devices[deviceID]
	if ok {
		delete(dm.devices, deviceID)
	}
	dm.mu.Unlock()

	if !ok {
		return fmt.Errorf("device %s not found", deviceID)
	}

	if info.Client.IsConnected() {
		info.Client.Disconnect()
	}
	if err := info.Client.Store.Delete(context.Background()); err != nil {
		log.Printf("[%s] store delete err: %v", deviceID, err)
	}
	info.Container.Close()

	dbFile := filepath.Join(dm.storeDir, deviceID+".db")
	_ = os.Remove(dbFile)
	_ = os.Remove(dbFile + "-wal")
	_ = os.Remove(dbFile + "-shm")

	return nil
}

func (dm *DeviceManager) Close() {
	dm.mu.Lock()
	defer dm.mu.Unlock()
	for id, info := range dm.devices {
		if info.Client.IsConnected() {
			info.Client.Disconnect()
		}
		info.Container.Close()
		log.Printf("[%s] disconnected on shutdown", id)
	}
}

// loadExistingDevices reconnects all persisted devices on startup.
func (dm *DeviceManager) loadExistingDevices() {
	pattern := filepath.Join(dm.storeDir, "*.db")
	files, err := filepath.Glob(pattern)
	if err != nil || len(files) == 0 {
		return
	}

	// Track phone numbers to avoid duplicate connections (same WA number on multiple devices)
	connectedPhones := make(map[string]string) // phone -> deviceID

	for _, f := range files {
		base := filepath.Base(f)
		deviceID := strings.TrimSuffix(base, ".db")

		dbPath := fmt.Sprintf("file:%s?_foreign_keys=on", f)
		container, err := sqlstore.New(context.Background(), "sqlite3", dbPath, waLog.Stdout("DB", "ERROR", true))
		if err != nil {
			log.Printf("[%s] load store err: %v", deviceID, err)
			continue
		}

		deviceStore, err := container.GetFirstDevice(context.Background())
		if err != nil || deviceStore.ID == nil {
			container.Close()
			log.Printf("[%s] skipped: no valid device in store", deviceID)
			continue
		}

		// Check for duplicate phone number
		phone := deviceStore.ID.User
		if existingID, dup := connectedPhones[phone]; dup {
			log.Printf("[%s] skipped: phone +%s already connected on device %s", deviceID, phone, existingID)
			container.Close()
			continue
		}

		client := whatsmeow.NewClient(deviceStore, waLog.Stdout("WA", "ERROR", true))
		info := &DeviceInfo{
			Client:    client,
			Container: container,
			Status:    StatusDisconnected,
		}

		dm.devices[deviceID] = info
		client.AddEventHandler(dm.makeEventHandler(deviceID))

		if err = client.Connect(); err != nil {
			log.Printf("[%s] reconnect err: %v", deviceID, err)
		} else {
			info.Status = StatusConnected
			if client.Store.ID != nil {
				info.PhoneNumber = client.Store.ID.User
				connectedPhones[info.PhoneNumber] = deviceID
			}
			log.Printf("[%s] reconnected (+%s)", deviceID, info.PhoneNumber)
		}
	}
}

func (dm *DeviceManager) makeEventHandler(deviceID string) func(interface{}) {
	return func(evt interface{}) {
		switch v := evt.(type) {
		case *events.Message:
			dm.handleIncoming(deviceID, v)
		case *events.Connected:
			dm.mu.Lock()
			if di, ok := dm.devices[deviceID]; ok {
				di.Status = StatusConnected
				di.LatestQR = ""
				if di.Client.Store.ID != nil {
					di.PhoneNumber = di.Client.Store.ID.User
				}
			}
			dm.mu.Unlock()
		case *events.Disconnected:
			dm.mu.Lock()
			if di, ok := dm.devices[deviceID]; ok {
				di.Status = StatusDisconnected
			}
			dm.mu.Unlock()
		}
	}
}

func (dm *DeviceManager) handleIncoming(deviceID string, evt *events.Message) {
	if evt.Info.IsFromMe || evt.Message == nil {
		return
	}

	chatJID := evt.Info.Chat
	isGroup := chatJID.Server == types.GroupServer

	// Extract text and mention list
	text := ""
	var mentionedJIDs []string
	if conv := evt.Message.GetConversation(); conv != "" {
		text = conv
	} else if ext := evt.Message.GetExtendedTextMessage(); ext != nil {
		text = ext.GetText()
		if ctx := ext.GetContextInfo(); ctx != nil {
			mentionedJIDs = ctx.GetMentionedJID()
		}
	}
	if text == "" {
		return
	}

	// Group messages: only process if the bot is explicitly @mentioned
	if isGroup {
		dm.mu.RLock()
		info, ok := dm.devices[deviceID]
		dm.mu.RUnlock()

		if !ok || info.Client.Store.ID == nil {
			return
		}

		botUser := info.Client.Store.ID.User // e.g. "628xxx"
		mentioned := false
		for _, jidStr := range mentionedJIDs {
			parsed, err := types.ParseJID(jidStr)
			if err == nil && parsed.User == botUser {
				mentioned = true
				break
			}
		}
		if !mentioned {
			log.Printf("[%s] group msg from +%s ignored (bot not mentioned)", deviceID, evt.Info.Sender.User)
			return
		}

		// Strip the @mention tag from the text so the agent gets a clean message
		text = strings.ReplaceAll(text, "@"+botUser, "")
		text = strings.TrimSpace(text)
		if text == "" {
			return
		}
	}

	from := "+" + evt.Info.Sender.User

	// chatID: always use evt.Info.Chat.String() — this is the authoritative JID for replies.
	// For DMs it can be "phone@s.whatsapp.net" or "phone@lid" (for LID-migrated accounts).
	// For groups it is "groupid@g.us".
	// Reconstructing from Sender.User loses the server info and breaks LID accounts.
	chatID := chatJID.String()

	payload := map[string]interface{}{
		"device_id": deviceID,
		"from":      from,
		"chat_id":   chatID,
		"message":   text,
		"timestamp": evt.Info.Timestamp.Unix(),
	}
	data, _ := json.Marshal(payload)

	go func() {
		resp, err := http.Post(dm.pythonWebhook, "application/json", bytes.NewReader(data))
		if err != nil {
			log.Printf("[%s] forward to python err: %v", deviceID, err)
			return
		}
		resp.Body.Close()
		if resp.StatusCode >= 400 {
			log.Printf("[%s] python webhook returned HTTP %d for msg from %s (chat: %s)", deviceID, resp.StatusCode, from, chatID)
		} else {
			log.Printf("[%s] forwarded msg from %s (chat: %s) to python", deviceID, from, chatID)
		}
	}()
}
