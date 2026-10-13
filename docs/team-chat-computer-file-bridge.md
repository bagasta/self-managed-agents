# Team Chat ↔ Computer File Bridge

Set these values in the API/worker environment:

```env
# URL reachable from inside the browser VM, not the browser on the owner's laptop.
COMPUTER_RUNTIME_FILE_BASE_URL=http://host.docker.internal:8000

# Host directory mounted as the browser VM's Downloads directory.
COMPUTER_RUNTIME_DOWNLOAD_DIR=/srv/managed-agents/computer-downloads
```

The VM must mount the same directory at its Downloads location. A Team Chat upload is exposed to that VM through a 15-minute, owner-and-room-scoped signed URL. The bot opens that URL with the computer, then uses the downloaded file in the browser.

After a bot run, new supported files in the mounted Downloads directory are moved into Team Chat storage and attached to that bot's reply. Allowed formats and size limits are the same as Team Chat uploads.
