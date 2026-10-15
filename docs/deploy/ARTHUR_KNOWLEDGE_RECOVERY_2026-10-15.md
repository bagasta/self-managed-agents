# Arthur knowledge upload recovery — 15 October 2026

## Outcome

Deployed to API and scheduler at **09:27 WIB** using
`managed-agents-app:arthur-knowledge-r2-20261015`.
Image manifest list: `sha256:4a5c1a1f4223fb6adb281f95416243fc46a7c8e63ad8ebd31dedb03d7838be0f`.

The previously requested PDF has been added to the correct, owner-scoped LapiBot
(`d91c36b9-9e09-4498-b549-ea3419db1db9`). All **12 pages**, **13,287 extracted
characters**, and **13 chunks** were processed. All 13 stored chunks have embeddings.
RAG is enabled; agent configuration version advanced from 7 to 8.

Both production prompt-context retrieval and the standalone `search_knowledge_base`
tool successfully returned the imported document. Document contents and credentials
are omitted from this report.

## Incident evidence

- At 09:11 WIB, WhatsApp saved the original PDF, but Mistral OCR returned HTTP 429.
- At 09:12 WIB, the user explicitly requested adding knowledge to LapiBot v1.
  Run `b55918ba-3ab7-4885-a2e5-b446f8d782ec` returned a document-reading blocker
  without calling `add_assistant_knowledge`.
- The original file remained in the session workspace. A read-only local extraction
  successfully read its 12 pages.
- During recovery, OCR again returned 429 after bounded retries. Local extraction
  succeeded, and the actual Arthur knowledge tool committed 13 embedded chunks at
  approximately 09:24 WIB.

The original failure was an OCR rate limit followed by Arthur stopping before the
knowledge tool. It was not a customer/owner tool permission rejection.

## Changes

- OCR requests retry transient 429/502/503/504 and transport errors, at most three
  attempts per request, with bounded delay. OCR retries reuse the uploaded file.
- Uploaded OCR files are cleaned up after both success and failure.
- Digital PDFs fall back to local extraction with pinned `pypdf==6.20.1` when OCR
  fails or returns empty text. Each page must contain sufficient extractable text;
  partial/scanned documents do not silently become incomplete knowledge.
- Arthur's prompt and attachment-failure hint direct it to call the knowledge tool
  for an explicitly requested import even when the earlier preview failed.
- The tool prefers the current upload directory and excludes generated
  `.extracted.txt` previews when selecting a file automatically.
- SHA-256 metadata and a target-agent row lock prevent a repeated import of the
  same complete file from adding duplicate chunks. All chunks commit atomically.
- A separate latent bug was found in the standalone RAG tool: it imported the
  nonexistent `document_service.search_documents`. Added a shared retrieval
  function that uses semantic search with keyword fallback. The production prompt
  retrieval now uses that function too, so an embedding outage can reach the
  keyword fallback. Vector queries use a savepoint to protect the outer transaction.

The standalone tool's broken import was discovered during verification; it was
not established as the cause of the original upload failure. Production agents
also receive RAG through prompt-context retrieval.

## Validation

**129 tests passed** covering knowledge uploads, bounded OCR retry/cleanup,
digital/scanned/partial PDF handling, original-file selection, deduplication,
ownership isolation, semantic/keyword retrieval fallback, Arthur P1/P2,
attachment evidence, agent updates, context isolation, and completed-write recovery.
One existing LangGraph deprecation warning remains.

Validation used an isolated container and SQLite fixtures. One initial P2 test
needed the dummy integration-service URL expected by its mocked Google client.
A later attempt ran against the earlier container source; the final complete
129-test run explicitly used the updated workspace source and passed.

Production checks after final deployment:

- API Docker health: healthy; public `/health`: HTTP 200.
- Scheduler health command: exit 0.
- Five deployed source files match the workspace SHA-256 hashes.
- Prompt RAG probe returned three relevant chunks from the imported PDF.
- Standalone RAG tool probe also returned that source document.
- No traceback, error-level startup log, or RAG-context failure was observed in
  the inspected post-deployment logs.
- No active runs, running jobs, or sending outbound messages were present at the
  deployment checks. WhatsApp services were not restarted.

The real import used the explicit prior WhatsApp request and verified the target
against the immediately preceding Arthur edit. It did not send a WhatsApp message,
rewrite the agent's interview instructions, or write to its Google spreadsheet.

## Files

- `app/core/domain/file_processor.py`
- `app/core/domain/document_service.py`
- `app/core/engine/prompt_builder.py`
- `app/api/wa_helpers.py`
- `arthur_v2/plugin.py`
- `requirements.txt`
- `tests/test_arthur_knowledge_recovery.py`
- `deploy/Dockerfile.arthur-knowledge-recovery`

## Rollback and limits

Previous image is retained as
`managed-agents-app:rollback-arthur-knowledge-20261015` (the P2 release).
To roll back the application only:

```sh
IMAGE_TAG=rollback-arthur-knowledge-20261015 docker compose -f deploy/docker-compose.prod.yml up -d --no-deps --no-build api scheduler
```

No schema migration was introduced. The imported documents remain valid after an
application rollback; rollback does not delete user knowledge. Earlier P2 warnings
about downgrading below P2 still apply.

Mistral's upstream rate limit itself was not removed. Scanned/image-only PDFs still
need an available OCR service; unreadable pages remain an explicit failure. The
tests and production retrieval probes verify these paths, without promising that
all future provider/network failures are impossible. A full user WhatsApp dialogue
was not automatically replayed.
