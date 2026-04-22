package main

import (
	"context"
	"encoding/base64"
	"fmt"
	"log"
	"os"
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

type WAStatus string

const (
	WAStatusWaitingQR    WAStatus = "waiting_qr"
	WAStatusConnected    WAStatus = "connected"
	WAStatusDisconnected WAStatus = "disconnected"
)

type IncomingMessage struct {
	From          string
	ChatID        string
	Text          string
	MediaType     string // "image" | "document" | "sticker" | ""
	MediaData     string // base64
	MediaFilename string
	MediaMimetype string
	Timestamp     int64
}

type WhatsAppClient struct {
	mu          sync.RWMutex
	client      *whatsmeow.Client
	container   *sqlstore.Container
	status      WAStatus
	phoneNumber string
	latestQR    string
	onMessage   func(msg IncomingMessage)
}

func NewWhatsAppClient(storeDir string, onMessage func(msg IncomingMessage)) (*WhatsAppClient, error) {
	if err := os.MkdirAll(storeDir, 0755); err != nil {
		return nil, fmt.Errorf("mkdir store: %w", err)
	}

	dbPath := fmt.Sprintf("file:%s/dev.db?_foreign_keys=on", storeDir)
	container, err := sqlstore.New(context.Background(), "sqlite3", dbPath, waLog.Stdout("DB", "ERROR", true))
	if err != nil {
		return nil, fmt.Errorf("sqlstore: %w", err)
	}

	deviceStore, err := container.GetFirstDevice(context.Background())
	if err != nil {
		deviceStore = container.NewDevice()
	}

	client := whatsmeow.NewClient(deviceStore, waLog.Stdout("WA", "ERROR", true))

	wa := &WhatsAppClient{
		client:    client,
		container: container,
		status:    WAStatusDisconnected,
		onMessage: onMessage,
	}

	client.AddEventHandler(wa.eventHandler)

	if client.Store.ID != nil {
		if err = client.Connect(); err != nil {
			log.Printf("wa-dev reconnect err: %v", err)
		} else {
			wa.status = WAStatusConnected
			wa.phoneNumber = client.Store.ID.User
			log.Printf("wa-dev reconnected (+%s)", wa.phoneNumber)
		}
	} else {
		wa.status = WAStatusWaitingQR
	}

	return wa, nil
}

func (wa *WhatsAppClient) Connect() (string, error) {
	wa.mu.RLock()
	status := wa.status
	wa.mu.RUnlock()

	if status == WAStatusConnected {
		return "", nil
	}

	if wa.client.Store.ID != nil {
		if err := wa.client.Connect(); err != nil {
			return "", fmt.Errorf("reconnect: %w", err)
		}
		return "", nil
	}

	qrChan, err := wa.client.GetQRChannel(context.Background())
	if err != nil {
		return "", fmt.Errorf("GetQRChannel: %w", err)
	}
	if err = wa.client.Connect(); err != nil {
		return "", fmt.Errorf("connect: %w", err)
	}

	firstQR := make(chan string, 1)
	go func() {
		for evt := range qrChan {
			switch evt.Event {
			case "code":
				png, encErr := qrcode.Encode(evt.Code, qrcode.Medium, 256)
				if encErr != nil {
					continue
				}
				b64 := base64.StdEncoding.EncodeToString(png)
				wa.mu.Lock()
				wa.latestQR = b64
				wa.mu.Unlock()
				select {
				case firstQR <- b64:
				default:
				}
			case "success":
				wa.mu.Lock()
				wa.status = WAStatusConnected
				wa.latestQR = ""
				if wa.client.Store.ID != nil {
					wa.phoneNumber = wa.client.Store.ID.User
				}
				wa.mu.Unlock()
			}
		}
	}()

	select {
	case qr := <-firstQR:
		return qr, nil
	case <-time.After(30 * time.Second):
		return "", fmt.Errorf("timeout waiting for QR")
	}
}

func (wa *WhatsAppClient) GetStatus() (WAStatus, string, string) {
	wa.mu.RLock()
	defer wa.mu.RUnlock()
	return wa.status, wa.phoneNumber, wa.latestQR
}

func (wa *WhatsAppClient) SendText(chatID, text string) error {
	if err := wa.checkConnected(); err != nil {
		return err
	}
	jid, err := parseJID(chatID)
	if err != nil {
		return err
	}
	_, err = wa.client.SendMessage(context.Background(), jid, &waE2E.Message{
		Conversation: proto.String(text),
	})
	return err
}

func (wa *WhatsAppClient) SendImage(to string, imageData []byte, caption, mimetype string) error {
	if err := wa.checkConnected(); err != nil {
		return err
	}
	jid, err := parseJID(to)
	if err != nil {
		return err
	}
	if mimetype == "" {
		mimetype = "image/jpeg"
	}
	resp, err := wa.client.Upload(context.Background(), imageData, whatsmeow.MediaImage)
	if err != nil {
		return fmt.Errorf("upload image: %w", err)
	}
	_, err = wa.client.SendMessage(context.Background(), jid, &waE2E.Message{
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
	})
	return err
}

func (wa *WhatsAppClient) SendDocument(to string, docData []byte, filename, caption, mimetype string) error {
	if err := wa.checkConnected(); err != nil {
		return err
	}
	jid, err := parseJID(to)
	if err != nil {
		return err
	}
	if mimetype == "" {
		mimetype = "application/octet-stream"
	}
	if filename == "" {
		filename = "file"
	}
	resp, err := wa.client.Upload(context.Background(), docData, whatsmeow.MediaDocument)
	if err != nil {
		return fmt.Errorf("upload document: %w", err)
	}
	_, err = wa.client.SendMessage(context.Background(), jid, &waE2E.Message{
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
	})
	return err
}

func (wa *WhatsAppClient) checkConnected() error {
	wa.mu.RLock()
	defer wa.mu.RUnlock()
	if wa.status != WAStatusConnected {
		return fmt.Errorf("not connected (status: %s)", wa.status)
	}
	return nil
}

func (wa *WhatsAppClient) Close() {
	if wa.client.IsConnected() {
		wa.client.Disconnect()
	}
	wa.container.Close()
}

func (wa *WhatsAppClient) eventHandler(evt interface{}) {
	switch v := evt.(type) {
	case *events.Message:
		wa.handleMessage(v)
	case *events.Connected:
		wa.mu.Lock()
		wa.status = WAStatusConnected
		wa.latestQR = ""
		if wa.client.Store.ID != nil {
			wa.phoneNumber = wa.client.Store.ID.User
		}
		wa.mu.Unlock()
		log.Printf("wa-dev connected (+%s)", wa.phoneNumber)
	case *events.Disconnected:
		wa.mu.Lock()
		wa.status = WAStatusDisconnected
		wa.mu.Unlock()
		log.Printf("wa-dev disconnected")
	case *events.LoggedOut:
		log.Printf("wa-dev logged out (reason: %v)", v.Reason)
		wa.mu.Lock()
		wa.status = WAStatusDisconnected
		wa.latestQR = ""
		wa.phoneNumber = ""
		_ = wa.client.Store.Delete(context.Background())
		wa.mu.Unlock()
	}
}

func (wa *WhatsAppClient) handleMessage(evt *events.Message) {
	if evt.Info.IsFromMe || evt.Message == nil {
		return
	}
	chatJID := evt.Info.Chat
	if chatJID.Server == types.BroadcastServer || chatJID.User == "status" {
		return
	}
	if chatJID.Server == types.GroupServer {
		return
	}

	msg := IncomingMessage{
		From:      "+" + evt.Info.Sender.User,
		ChatID:    chatJID.String(),
		Timestamp: evt.Info.Timestamp.Unix(),
	}

	switch {
	case evt.Message.GetConversation() != "":
		msg.Text = evt.Message.GetConversation()

	case evt.Message.GetExtendedTextMessage() != nil:
		msg.Text = evt.Message.GetExtendedTextMessage().GetText()

	case evt.Message.GetImageMessage() != nil:
		img := evt.Message.GetImageMessage()
		raw, err := wa.client.Download(context.Background(), img)
		if err == nil {
			msg.MediaData = base64.StdEncoding.EncodeToString(raw)
			msg.MediaType = "image"
			msg.MediaMimetype = img.GetMimetype()
			ext := mimeToExt(img.GetMimetype(), ".jpg")
			msg.MediaFilename = "image" + ext
		}
		msg.Text = img.GetCaption()
		if msg.Text == "" {
			msg.Text = "[Gambar]"
		}

	case evt.Message.GetDocumentMessage() != nil:
		doc := evt.Message.GetDocumentMessage()
		raw, err := wa.client.Download(context.Background(), doc)
		if err == nil {
			msg.MediaData = base64.StdEncoding.EncodeToString(raw)
			msg.MediaType = "document"
			msg.MediaMimetype = doc.GetMimetype()
			msg.MediaFilename = doc.GetFileName()
			if msg.MediaFilename == "" {
				msg.MediaFilename = "file"
			}
		}
		msg.Text = doc.GetCaption()
		if msg.Text == "" {
			msg.Text = fmt.Sprintf("[Dokumen: %s]", doc.GetFileName())
		}

	case evt.Message.GetAudioMessage() != nil:
		audio := evt.Message.GetAudioMessage()
		raw, err := wa.client.Download(context.Background(), audio)
		if err == nil {
			msg.MediaData = base64.StdEncoding.EncodeToString(raw)
			msg.MediaType = "audio"
			msg.MediaMimetype = audio.GetMimetype()
			msg.MediaFilename = "audio.ogg"
		}
		msg.Text = "[Audio]"

	case evt.Message.GetStickerMessage() != nil:
		sticker := evt.Message.GetStickerMessage()
		raw, err := wa.client.Download(context.Background(), sticker)
		if err == nil {
			msg.MediaData = base64.StdEncoding.EncodeToString(raw)
			msg.MediaType = "sticker"
			msg.MediaMimetype = "image/webp"
			msg.MediaFilename = "sticker.webp"
		}
		msg.Text = "[Sticker]"

	default:
		return
	}

	if wa.onMessage != nil {
		go wa.onMessage(msg)
	}
}

func mimeToExt(mime, fallback string) string {
	switch {
	case strings.Contains(mime, "png"):
		return ".png"
	case strings.Contains(mime, "webp"):
		return ".webp"
	case strings.Contains(mime, "gif"):
		return ".gif"
	default:
		return fallback
	}
}

func parseJID(s string) (types.JID, error) {
	if strings.Contains(s, "@") {
		parsed, err := types.ParseJID(s)
		if err != nil {
			return types.JID{}, fmt.Errorf("invalid JID %q: %w", s, err)
		}
		return parsed, nil
	}
	phone := strings.TrimPrefix(s, "+")
	return types.NewJID(phone, types.DefaultUserServer), nil
}
