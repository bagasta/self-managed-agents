---
name: arthur-files-knowledge
description: Define and verify file handling, document knowledge, RAG, image understanding, and generated artifact delivery for an agent. Use when users mention documents, PDFs, spreadsheets, images, uploads, reports, generated files, websites as sources, or knowledge bases.
---

# Arthur Files and Knowledge

Separate four capabilities: receive files, read/extract files, generate files, and send files through WhatsApp. Confirm only capabilities relevant to the actual workflow.

## Input Routing

- Plain chat: primary Arthur model.
- PDF/DOCX/PPTX document evidence: Mistral Document AI using `mistral-ocr-latest`.
- JPEG/PNG/WebP visual evidence: `openai/gpt-4.1-mini`.
- Scanned PDF remains a document route.
- TXT/MD/CSV may use deterministic parsing but still produce document evidence.

Attachment processors only extract evidence. They never select builder tools, change build state, or make final decisions.

## Workflow

1. Confirm which file types enter or leave the workflow and their purpose.
2. Confirm whether content becomes durable agent knowledge or is used for one turn only.
3. Validate MIME type, extension, size, tenant ownership, and retention policy.
4. Preserve filename/page/section provenance and extraction warnings.
5. Configure only the required RAG, sandbox, subagent, and WhatsApp media capabilities.
6. Test the exact receive/read/generate/send path required by the agent.

## Failure Rules

- Never claim to have read an attachment when extraction failed.
- Never infer file contents from filename, caption, or an old workspace artifact.
- Never silently switch document/image models after provider failure.
- Return `attachment_processing_failed` with the affected file and a safe retry/re-upload instruction.
- Never ask the file-capability question twice after it has been resolved.
