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

	// If device exists but store has no valid session (e.g. logged out from phone),
	// tear it down so we can do a fresh QR scan below.
	if ok && existing.Client.Store.ID == nil {
		log.Printf("[%s] device has no valid session (logged out) — reinitialising", deviceID)
		if existing.Client.IsConnected() {
			existing.Client.Disconnect()
		}
		existing.Container.Close()
		dm.mu.Lock()
		delete(dm.devices, deviceID)
		dm.mu.Unlock()
		ok = false
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
				png, genErr := qrcode.Encode(evt.Code, qrcode.High, 512)
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

	// Stop typing indicator before sending the actual reply.
	_ = info.Client.SendChatPresence(context.Background(), jid, types.ChatPresencePaused, types.ChatPresenceMediaText)

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
		case *events.LoggedOut:
			log.Printf("[%s] logged out (reason: %v) — clearing session store", deviceID, v.Reason)
			dm.mu.Lock()
			di, ok := dm.devices[deviceID]
			if ok {
				di.Status = StatusDisconnected
				di.LatestQR = ""
				di.PhoneNumber = ""
				// Delete the persisted session so CreateDevice can do a fresh QR scan
				if err := di.Client.Store.Delete(context.Background()); err != nil {
					log.Printf("[%s] store.Delete after logout err: %v", deviceID, err)
				}
			}
			dm.mu.Unlock()
		}
	}
}

// resolveJID resolves a phone number or JID string to the correct WhatsApp JID,
// including @lid resolution for newer WA accounts. Uses IsOnWhatsApp for phone lookups.
func resolveJID(client *whatsmeow.Client, to string) (types.JID, error) {
	if strings.Contains(to, "@") {
		parsed, err := types.ParseJID(to)
		if err != nil {
			return types.JID{}, fmt.Errorf("invalid JID %q: %w", to, err)
		}
		if parsed.Device > 0 {
			return types.JID{}, fmt.Errorf("cannot send to AD JID %q — use non-device JID", to)
		}
		return parsed, nil
	}
	phone := strings.TrimPrefix(to, "+")
	// Query WA servers for the real JID — resolves @lid accounts correctly.
	results, err := client.IsOnWhatsApp(context.Background(), []string{phone})
	if err == nil && len(results) > 0 && results[0].IsIn {
		return results[0].JID, nil
	}
	// Fallback to @s.whatsapp.net if lookup fails
	return types.NewJID(phone, types.DefaultUserServer), nil
}

// SendImage uploads and sends an image to a WhatsApp number.
// imageData is the raw image bytes, mimetype e.g. "image/jpeg".
func (dm *DeviceManager) SendImage(deviceID, to string, imageData []byte, caption, mimetype string) error {
	dm.mu.RLock()
	info, ok := dm.devices[deviceID]
	dm.mu.RUnlock()

	if !ok {
		return fmt.Errorf("device %s not found", deviceID)
	}
	if info.Status != StatusConnected {
		return fmt.Errorf("device %s not connected (status: %s)", deviceID, info.Status)
	}
	if info.Client.Store.ID == nil {
		return fmt.Errorf("device %s: no valid WA session (needs re-scan QR)", deviceID)
	}

	jid, err := resolveJID(info.Client, to)
	if err != nil {
		return err
	}

	if mimetype == "" {
		mimetype = "image/jpeg"
	}

	// Upload to WhatsApp servers
	resp, err := info.Client.Upload(context.Background(), imageData, whatsmeow.MediaImage)
	if err != nil {
		return fmt.Errorf("upload image: %w", err)
	}

	msg := &waE2E.Message{
		ImageMessage: &waE2E.ImageMessage{
			Caption:       proto.String(caption),
			Mimetype:      proto.String(mimetype),
			URL:           proto.String(resp.URL),
			DirectPath:    proto.String(resp.DirectPath),
			MediaKey:      resp.MediaKey,
			FileEncSHA256: resp.FileEncSHA256,
			FileSHA256:    resp.FileSHA256,
			FileLength:    proto.Uint64(resp.FileLength),
		},
	}

	_, err = info.Client.SendMessage(context.Background(), jid, msg)
	if err != nil {
		log.Printf("[%s] send image to %s failed: %v", deviceID, to, err)
	}
	return err
}

// SendDocument uploads and sends a document to a WhatsApp number.
// docData is the raw file bytes; filename is the display name; mimetype e.g. "application/pdf".
func (dm *DeviceManager) SendDocument(deviceID, to string, docData []byte, filename, caption, mimetype string) error {
	dm.mu.RLock()
	info, ok := dm.devices[deviceID]
	dm.mu.RUnlock()

	if !ok {
		return fmt.Errorf("device %s not found", deviceID)
	}
	if info.Status != StatusConnected {
		return fmt.Errorf("device %s not connected (status: %s)", deviceID, info.Status)
	}
	if info.Client.Store.ID == nil {
		return fmt.Errorf("device %s: no valid WA session (needs re-scan QR)", deviceID)
	}

	jid, err := resolveJID(info.Client, to)
	if err != nil {
		return err
	}

	if mimetype == "" {
		mimetype = "application/octet-stream"
	}
	if filename == "" {
		filename = "file"
	}

	resp, err := info.Client.Upload(context.Background(), docData, whatsmeow.MediaDocument)
	if err != nil {
		return fmt.Errorf("upload document: %w", err)
	}

	msg := &waE2E.Message{
		DocumentMessage: &waE2E.DocumentMessage{
			Caption:       proto.String(caption),
			Mimetype:      proto.String(mimetype),
			FileName:      proto.String(filename),
			URL:           proto.String(resp.URL),
			DirectPath:    proto.String(resp.DirectPath),
			MediaKey:      resp.MediaKey,
			FileEncSHA256: resp.FileEncSHA256,
			FileSHA256:    resp.FileSHA256,
			FileLength:    proto.Uint64(resp.FileLength),
		},
	}

	_, err = info.Client.SendMessage(context.Background(), jid, msg)
	if err != nil {
		log.Printf("[%s] send document to %s failed: %v", deviceID, to, err)
	}
	return err
}

func (dm *DeviceManager) handleIncoming(deviceID string, evt *events.Message) {
	if evt.Info.IsFromMe || evt.Message == nil {
		return
	}

	chatJID := evt.Info.Chat

	// Skip broadcast and WA Status messages
	if chatJID.Server == types.BroadcastServer || chatJID.User == "status" {
		log.Printf("[%s] ignored broadcast/status msg from %s (chat: %s)", deviceID, evt.Info.Sender.User, chatJID.String())
		return
	}

	isGroup := chatJID.Server == types.GroupServer

	// Extract text, mention list, and media info
	text := ""
	var mentionedJIDs []string
	mediaType := ""
	mediaData := ""
	mediaFilename := ""

	if conv := evt.Message.GetConversation(); conv != "" {
		text = conv
	} else if ext := evt.Message.GetExtendedTextMessage(); ext != nil {
		text = ext.GetText()
		if ctx := ext.GetContextInfo(); ctx != nil {
			mentionedJIDs = ctx.GetMentionedJID()
		}
	} else if img := evt.Message.GetImageMessage(); img != nil {
		// Image message
		dm.mu.RLock()
		info, ok := dm.devices[deviceID]
		dm.mu.RUnlock()
		if ok {
			raw, err := info.Client.Download(context.Background(), img)
			if err == nil {
				mediaData = base64.StdEncoding.EncodeToString(raw)
				mediaType = "image"
				ext := ".jpg"
				if mt := img.GetMimetype(); strings.Contains(mt, "png") {
					ext = ".png"
				} else if strings.Contains(mt, "webp") {
					ext = ".webp"
				}
				mediaFilename = "image" + ext
			} else {
				log.Printf("[%s] download image err: %v", deviceID, err)
			}
		}
		if cap := img.GetCaption(); cap != "" {
			text = cap
		} else {
			text = "[Gambar]"
		}
	} else if doc := evt.Message.GetDocumentMessage(); doc != nil {
		// Document message
		dm.mu.RLock()
		info, ok := dm.devices[deviceID]
		dm.mu.RUnlock()
		if ok {
			raw, err := info.Client.Download(context.Background(), doc)
			if err == nil {
				mediaData = base64.StdEncoding.EncodeToString(raw)
				mediaType = "document"
				mediaFilename = doc.GetFileName()
				if mediaFilename == "" {
					mediaFilename = "file"
				}
			} else {
				log.Printf("[%s] download document err: %v", deviceID, err)
			}
		}
		if cap := doc.GetCaption(); cap != "" {
			text = cap
		} else {
			text = fmt.Sprintf("[Dokumen: %s]", doc.GetFileName())
		}
	} else if sticker := evt.Message.GetStickerMessage(); sticker != nil {
		dm.mu.RLock()
		info, ok := dm.devices[deviceID]
		dm.mu.RUnlock()
		if ok {
			raw, err := info.Client.Download(context.Background(), sticker)
			if err == nil {
				mediaData = base64.StdEncoding.EncodeToString(raw)
				mediaType = "sticker"
				mediaFilename = "sticker.webp"
			}
		}
		text = "[Sticker]"
	} else if audio := evt.Message.GetAudioMessage(); audio != nil {
		// Voice note (PTT) atau file audio biasa
		dm.mu.RLock()
		info, ok := dm.devices[deviceID]
		dm.mu.RUnlock()
		if ok {
			raw, err := info.Client.Download(context.Background(), audio)
			if err == nil {
				mediaData = base64.StdEncoding.EncodeToString(raw)
				if audio.GetPTT() {
					mediaType = "ptt" // push-to-talk / voice note
					mediaFilename = "voice.ogg"
					text = "[Voice note]"
				} else {
					mediaType = "audio" // file audio biasa
					mediaFilename = "audio.ogg"
					text = "[Audio]"
				}
			} else {
				log.Printf("[%s] download audio err: %v", deviceID, err)
			}
		}
	}

	// Skip if no text and no media
	if text == "" && mediaType == "" {
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

		botJID := *info.Client.Store.ID
		botUser := botJID.User // phone number e.g. "628xxx"

		// Also resolve bot's LID so we can match @lid mentions
		botLID := ""
		if lidJID, err := info.Client.Store.LIDs.GetLIDForPN(context.Background(), botJID); err == nil {
			botLID = lidJID.User
		}

		log.Printf("[%s] group mention check: botUser=%s botLID=%s mentionedJIDs=%v", deviceID, botUser, botLID, mentionedJIDs)
		mentioned := false
		for _, jidStr := range mentionedJIDs {
			parsed, err := types.ParseJID(jidStr)
			if err != nil {
				continue
			}
			if parsed.User == botUser || (botLID != "" && parsed.User == botLID) {
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
		if text == "" && mediaType == "" {
			return
		}
	}

	from := "+" + evt.Info.Sender.User

	// chatID: always use evt.Info.Chat.String() — this is the authoritative JID for replies.
	// For DMs it can be "phone@s.whatsapp.net" or "phone@lid" (for LID-migrated accounts).
	// For groups it is "groupid@g.us".
	// Reconstructing from Sender.User loses the server info and breaks LID accounts.
	chatID := chatJID.String()

	// phone_from: resolved phone number, always in "+phone" format.
	// For LID accounts, Sender.User contains a LID number (not the phone number).
	// We attempt to resolve it using the local LID→PN map. If the map has the entry
	// (populated when WA sends contact data), phoneFrom will be the real phone number.
	// Falls back to `from` if resolution fails or is unavailable.
	phoneFrom := from
	dm.mu.RLock()
	resolveInfo, resolveOk := dm.devices[deviceID]
	dm.mu.RUnlock()
	if resolveOk && resolveInfo.Client.Store != nil {
		if pnJID, err := resolveInfo.Client.Store.LIDs.GetPNForLID(context.Background(), evt.Info.Sender); err == nil && pnJID.User != "" {
			phoneFrom = "+" + pnJID.User
			log.Printf("[%s] resolved LID %s -> phone %s", deviceID, from, phoneFrom)
		}
	}

	// Send typing indicator immediately so the user sees "typing..." while AI processes.
	dm.mu.RLock()
	typingInfo, typingOk := dm.devices[deviceID]
	dm.mu.RUnlock()
	if typingOk {
		_ = typingInfo.Client.SendChatPresence(context.Background(), chatJID, types.ChatPresenceComposing, types.ChatPresenceMediaText)
	}

	payload := map[string]interface{}{
		"device_id":      deviceID,
		"from":           from,
		"phone_from":     phoneFrom, // resolved phone number (same as from for non-LID accounts)
		"chat_id":        chatID,
		"message":        text,
		"timestamp":      evt.Info.Timestamp.Unix(),
		"push_name":      evt.Info.PushName,
		"media_type":     mediaType,
		"media_data":     mediaData,
		"media_filename": mediaFilename,
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
