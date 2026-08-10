# QA Comparison Arthur — 28 Juli 2026

Status dokumen: diagnosis terhadap source terbaru dan replay percakapan Minsel
Branch yang diperiksa: `agent/guard-arthur-media-sources`
HEAD saat pemeriksaan: `98910cf` — `arthur-progressive-v3` / `arthur-kernel-v13`

## 1. Tujuan QA

Dokumen ini membandingkan bagian Arthur yang sudah bekerja baik dengan bagian yang belum memenuhi kualitas yang diharapkan. Scope QA mencakup:

1. flow bicara Arthur dengan user;
2. discovery dan pembentukan brief;
3. orkestrasi progressive skill;
4. pemilihan, pemanggilan, dan verifikasi tool;
5. pembuatan agent;
6. integrasi Google Workspace;
7. handoff ke demo atau nomor khusus;
8. kualitas agent hasil buatan Arthur;
9. coverage test dan release gate.

Arti status:

- `OK`: perilaku sudah memiliki kontrak yang jelas, implementasi deterministik yang memadai, dan bukti test atau runtime.
- `PARTIAL`: fondasi sudah ada, tetapi masih bergantung pada LLM, memiliki gap UX, atau belum diuji end-to-end.
- `NOT OK`: perilaku utama tidak tersedia, tidak terukur, atau bukti percakapan menunjukkan kegagalan.

## 2. Kesimpulan QA

Arthur saat ini lebih kuat sebagai **orchestrator teknis pembuatan agent** daripada sebagai **konsultan yang menggali kebutuhan bisnis**.

Yang sudah kuat:

- state discovery persisten;
- tool dibatasi berdasarkan skill dan workflow state;
- planning dan create memiliki recovery;
- konfirmasi dan evidence lebih aman;
- file capability lebih konsisten;
- create, verify, Google setup, dan demo memiliki jalur tool yang jelas;
- klaim keberhasilan tanpa tool result mulai dibatasi.

Yang masih lemah:

- percakapan terasa seperti formulir;
- pertanyaan tidak cukup adaptif terhadap jawaban user;
- discovery berhenti pada minimum buildable brief, bukan minimum useful brief;
- kualitas konteks bisnis tidak menjadi release gate;
- tidak ada QA yang menguji apakah agent hasil creation komunikatif dan mampu menyelesaikan workflow customer;
- beberapa test discovery tidak sinkron dengan kebijakan concise intake terbaru.

## 3. Scorecard utama

| Area QA | Status | Ringkasan |
| --- | --- | --- |
| Greeting dan pembukaan | `PARTIAL` | Greeting sederhana tidak lagi memicu planning recovery, tetapi Arthur belum menjelaskan perannya dan proses interview secara natural pada percakapan Minsel. |
| Pemahaman intent awal | `PARTIAL` | Arthur dapat membedakan pain point dari nama fitur, tetapi belum melakukan reframing workflow bisnis secara mendalam. |
| Gaya percakapan | `NOT OK` | Pertanyaan kanonis dibacakan seperti form; acknowledgment, refleksi, dan transisi kontekstual sangat minim. |
| Adaptive probing | `NOT OK` | Arthur mengejar field berikutnya, bukan menggali konsekuensi dari jawaban user. |
| Satu pertanyaan per turn | `OK` | Guard dan discovery contract konsisten mengeluarkan satu pertanyaan prioritas. |
| Deduplikasi pertanyaan | `PARTIAL` | Manifest dan question history tersedia, tetapi pertanyaan eskalasi masih dapat terulang ketika bentuk ekstraksi belum lolos validator. |
| Persistence discovery | `OK` | Fakta, evidence, confirmation, integration state, dan build progress memiliki state persisten. |
| Evidence anti-asumsi | `OK` | Field material dapat ditolak bila tidak memiliki bukti dari pesan user. |
| Kelengkapan brief teknis | `PARTIAL` | Tujuan, audience, task, larangan, fallback, eskalasi, file, dan integrasi dijaga; detail operasional banyak menjadi opsional. |
| Kelengkapan brief bisnis | `NOT OK` | Produk, katalog, harga, stok, order state, data schema, payment policy, dan handoff experience tidak wajib digali. |
| Final summary dan confirmation | `PARTIAL` | Konfirmasi terikat manifest dan lebih tahan loop, tetapi summary dapat mengandung elaborasi yang tidak pernah dibahas eksplisit. |
| Primary skill routing | `OK` | Discovery, create, edit, WhatsApp channel, subscription, lifecycle, Google, dan file memiliki scope yang terpisah. |
| Progressive tool exposure | `OK` | Tool allowlist per skill dan mixin sudah eksplisit; Google/file dapat dikomposisikan. |
| Planning orchestration | `OK` | Turn discovery/create yang tidak memanggil `plan_agent` memiliki recovery deterministik. |
| Create continuation | `OK` | Runtime mendeteksi plan/composition yang berhenti sebelum `create_agent` dan mencoba melanjutkan. |
| Blueprint/manual/instruction/soul chain | `PARTIAL` | Chain tersedia dan terstruktur, tetapi kualitas output dibatasi oleh kualitas discovery input. |
| Config validation | `PARTIAL` | Schema, tool, safety, placeholder, approval, dan asumsi diperiksa; usefulness dan conversational quality tidak dinilai. |
| Create dan verify | `OK` | `create_agent` diikuti kontrak verifikasi dan tidak boleh diklaim sukses hanya dari teks model. |
| Google OAuth | `OK` | Auth link dan state integrasi tersedia; auth tidak lagi dianggap sama dengan resource siap. |
| Google Sheet readiness | `PARTIAL` | Ada bootstrap dan write/read verification, tetapi perlu canary runtime untuk setiap jenis workflow dan schema bisnis. |
| Demo `wa.me` dan kode | `OK` | Opsi demo, termasuk jawaban `1`, dipetakan ke `create_wa_dev_trial_link`; reply menggunakan hasil tool. |
| Nomor khusus / QR | `PARTIAL` | Jalur tool tersedia, tetapi perlu QA nyata terhadap device ownership, idempotency, dan kegagalan scan. |
| Kualitas agent hasil creation | `NOT OK` | Belum ada acceptance test yang menguji agent hasil Arthur lewat percakapan customer end-to-end. |
| Runtime freshness | `PARTIAL` | Version metadata dan health gate sudah ada, tetapi source terbaru belum otomatis berarti seed/runtime produksi terbaru. |
| Automated test consistency | `PARTIAL` | Fast-intake suite lulus, tetapi discovery suite saat ini memiliki tiga kegagalan. |

## 4. QA flow bicara Arthur

### 4.1 Yang sudah OK

| Check | Bukti | Expected result |
| --- | --- | --- |
| Greeting sederhana tidak menjalankan planning | `_is_non_actionable_builder_greeting()` mengecualikan `halo`, `hai`, `pagi`, dan variasinya dari planning recovery. | User menerima sapaan natural, bukan teks internal seperti “saya panggil perencanaan”. |
| Arthur meminta pain point, bukan hanya nama fitur | Validator menolak `problem` yang hanya berupa jenis agent atau fitur. | Brief dimulai dari masalah user. |
| Satu pertanyaan pada satu pesan | Validator dapat memiliki beberapa missing field, tetapi reply guard hanya mengambil pertanyaan prioritas pertama. | WhatsApp tidak dibanjiri checklist panjang dalam satu pesan. |
| Jawaban multi-field dapat disimpan | Discovery contract mengizinkan semua fakta sukarela disimpan sekaligus. | User tidak ditanya kembali tentang informasi yang sudah diberikan. |
| Summary membutuhkan confirmation | Manifest dan confirmation evidence dipersistenkan. | Create tidak berjalan hanya karena model menganggap user sudah setuju. |

### 4.2 Yang belum OK

| Check | Temuan pada Minsel | Risiko |
| --- | --- | --- |
| Arthur menjelaskan peran dan proses | Setelah “Halo”, Arthur langsung bertanya kebutuhan tanpa menjelaskan bahwa ia akan memahami workflow, merangkum, lalu membuat agent. | User tidak memiliki mental model tentang proses dan jenis jawaban yang diperlukan. |
| Acknowledgment kontekstual | Setelah jawaban user, Arthur umumnya langsung mengeluarkan pertanyaan berikutnya. | Percakapan terasa seperti form dan user tidak tahu apakah jawabannya dipahami dengan benar. |
| Reframing | “CS AI untuk handle pembeli” dianggap cukup sebagai tugas utama. | Label pekerjaan diterima sebagai workflow. |
| Adaptive probing | Setelah Google Sheets disebut, Arthur tidak bertanya data apa yang dicatat, kapan dicatat, atau siapa yang memverifikasi pembayaran. | Integrasi aktif tetapi workflow bisnis salah atau kosong. |
| Menjelaskan istilah | Pertanyaan capability menggunakan opsi file yang panjang; user menjawab “a dan b”. | User menjawab kode opsi tanpa benar-benar memahami konsekuensi runtime. |
| Clarification yang presisi | Ketika eskalasi belum lengkap, Arthur mengulang seluruh pertanyaan yang sama. | User frustrasi karena merasa jawabannya diabaikan. |
| Confirmation grounded | Summary menambahkan contoh “bukti transfer, screenshot, dll” yang tidak pernah dinyatakan. | User dapat menyetujui asumsi tersembunyi karena summary terdengar masuk akal. |
| Conversational tone | Tidak ada pemeriksaan otomatis atas kehangatan, kejelasan, jargon, panjang pesan, atau transisi. | Arthur dapat technically correct tetapi tidak nyaman digunakan. |

### 4.3 Acceptance criteria flow bicara

Arthur baru dinilai `PASS` bila:

1. membuka percakapan dengan peran dan proses singkat;
2. mengakui jawaban user dengan refleksi faktual, maksimal satu kalimat;
3. tidak membaca pertanyaan kanonis mentah bila pertanyaan dapat dibuat lebih kontekstual;
4. bertanya ulang hanya tentang komponen yang belum lengkap;
5. menjelaskan istilah teknis dengan contoh bisnis user;
6. tidak memasukkan fakta baru ke summary tanpa label “usulan/default”;
7. dapat membedakan label use case dari workflow operasional;
8. mempertahankan satu pertanyaan utama per turn tanpa kehilangan kedalaman.

## 5. QA discovery dan brief

### 5.1 Yang sudah OK

| Capability | Status QA |
| --- | --- |
| Memisahkan personal dan bisnis | `OK` |
| Nama agent harus disetujui user | `OK` |
| Audience dikumpulkan | `OK` |
| Tugas konkret memiliki field sendiri | `OK`, tetapi enforcement semantik masih lemah |
| Larangan material dikumpulkan | `OK` |
| Unknown handling dikumpulkan | `OK` |
| Eskalasi bisnis membutuhkan trigger, recipient, dan nomor | `OK` secara schema |
| File capability memiliki nilai kanonis | `OK` |
| Integrasi eksplisit dikumpulkan | `OK` |
| Evidence dari pesan user dipertahankan | `OK` |
| Confirmation terikat manifest | `OK` |

### 5.2 Yang belum OK

Kebijakan concise intake menjadikan field berikut opsional:

- `allowed_actions`;
- `tone_style`;
- `ideal_conversations`;
- `avoided_conversations`;
- `knowledge_sources`;
- `daily_chat_volume`;
- `expected_outputs`;
- `vision_requirement`;
- `go_live_approver`;
- `sensitive_data_policy`;
- `whatsapp_scale`.

Keputusan ini mengurangi stall, tetapi beberapa field seharusnya **conditional-required**, bukan selalu opsional:

| Kondisi use case | Field yang harus otomatis wajib |
| --- | --- |
| CS menjawab produk/harga/stok | knowledge source dan freshness policy |
| Agent menerima bukti pembayaran | aturan membaca bukti, authority verifikasi, dan approval manusia |
| Agent menulis Google Sheets | trigger penulisan, spreadsheet/table, kolom wajib, idempotency, dan error handling |
| Customer eksternal | tone, disclosure, privacy, dan handoff message |
| Agent melakukan transaksi/order | state order, required customer data, success criteria, cancellation/refund boundary |
| Agent memakai media | jenis media, tindakan yang diizinkan, dan fallback jika media tidak terbaca |
| Agent mengeskalasi | trigger, penerima, destination, summary format, attachment policy, dan pesan ke customer |

### 5.3 Minimum useful brief untuk CS WhatsApp

Agent CS tidak boleh masuk ke planning `ready` sebelum tersedia:

1. business/domain description;
2. target customer;
3. daftar intent customer utama;
4. workflow untuk setiap intent prioritas;
5. source of truth;
6. data yang harus dikumpulkan;
7. keputusan yang boleh dan tidak boleh dibuat;
8. escalation contract;
9. integration side effects;
10. tone dan minimal dua contoh dialog yang sudah disetujui atau jelas diberi label sebagai default;
11. definition of done untuk percakapan;
12. fallback ketika source/tool gagal.

## 6. QA orkestrasi skill

### 6.1 Yang sudah OK

Arthur memiliki primary skill dan mixin yang cukup jelas:

| Skill | Tool utama yang diekspos | Status |
| --- | --- | --- |
| `arthur-discovery` | capability, preset, planning, subscription/read agent | `OK` |
| `arthur-create-agent` | blueprint, manual, instructions, soul, validation, create, verify | `OK` |
| `arthur-edit-agent` | read, update, verify, recompose, knowledge | `OK` |
| `arthur-whatsapp-demo-channel` | device read, trial link, QR | `OK` |
| `arthur-subscription-payment` | subscription dan payment link | `OK` |
| `arthur-lifecycle-safety` | read, delete, renew, verify | `OK` |
| `arthur-google-workspace` mixin | auth, update, verify, resource tools | `OK/PARTIAL` |
| `arthur-files-knowledge` mixin | update, verify, knowledge, recompose | `OK/PARTIAL` |

Kekuatan desain saat ini:

- seluruh builder tool tidak ditampilkan pada setiap turn;
- tool exposure mengikuti primary skill;
- Google dan file dapat ditambahkan sebagai mixin;
- channel action tertentu dapat dibuka bersama create ketika user memilih demo atau nomor khusus;
- ready-plan continuation menghapus `plan_agent` agar tidak kembali ke discovery;
- daftar tool eligible dan state-gated diinjeksi ke prompt agar model tidak salah menyebut tool hilang.

### 6.2 Yang belum OK

| Gap | Dampak |
| --- | --- |
| Intent dan primary skill masih sebagian bergantung pada regex serta pesan terakhir | Bahasa user yang tidak cocok pola dapat masuk skill yang salah. |
| Kualitas fakta yang masuk skill tidak dinilai | Skill create dapat bekerja sempurna dengan brief yang buruk. |
| Skill discovery mengatur urutan field, bukan strategi konsultasi | Progressive disclosure menjadi progressive form filling. |
| Tidak ada confidence atau ambiguity policy untuk routing | Runtime tidak selalu tahu kapan harus klarifikasi intent sebelum memilih skill. |
| Tidak ada transition QA lintas skill berbasis replay nyata | User dapat berpindah dari OAuth ke demo, edit, atau diagnosis dalam satu pesan dan memicu kombinasi yang belum diuji. |
| Skill contract tidak memiliki quality score output | Planning hanya mengenal lengkap/tidak lengkap, bukan cukup berguna/tidak cukup berguna. |

### 6.3 Acceptance criteria orkestrasi skill

1. Setiap turn menyimpan `detected_intent`, `primary_skill`, `mixin_skills`, `workflow_state`, dan alasan routing yang dapat diaudit.
2. Ambiguity tinggi harus menghasilkan satu clarification, bukan memilih skill secara agresif.
3. Complaint terhadap agent existing tidak boleh masuk new-agent discovery.
4. Google/file/channel mixin harus dapat aktif bersama tanpa menghilangkan tool yang diperlukan.
5. Transition `discovery -> confirmation -> plan -> compose -> validate -> create -> verify -> setup -> channel` harus memiliki integration test.
6. Skill create harus menolak brief dengan `business_readiness=insufficient`, walaupun schema minimum lengkap.

## 7. QA orkestrasi tool

### 7.1 Yang sudah OK

| Tahap | Orkestrasi yang tersedia | Status |
| --- | --- | --- |
| Capability check | `get_platform_capabilities`, `get_presets` | `OK` |
| Planning | `plan_agent` wajib pada discovery/create | `OK` |
| Blueprint | `compose_agent_blueprint` | `OK/PARTIAL` |
| SOP | `compose_agent_operating_manual` | `OK/PARTIAL` |
| System instructions | `compose_agent_instructions` | `OK/PARTIAL` |
| Personality | `compose_agent_soul` | `OK/PARTIAL` |
| Config gate | `validate_agent_config` | `OK/PARTIAL` |
| Creation | `create_agent` | `OK` |
| Verification | `verify_agent`, `get_agent_detail` | `OK` |
| Google auth | `generate_google_auth_link` | `OK` |
| Google resource setup | create/select/write/read/bind resource | `PARTIAL` |
| Demo | `create_wa_dev_trial_link` | `OK` |
| Dedicated number | device list dan `send_agent_wa_qr` | `PARTIAL` |

Kekuatan penting:

- runtime mendeteksi turn discovery/create yang tidak pernah memanggil `plan_agent`;
- runtime mendeteksi chain yang berhenti setelah planning/composition dan belum memanggil `create_agent`;
- reply guard menolak sebagian klaim “sudah jadi” tanpa evidence tool;
- latest plan result lebih otoritatif daripada clarification lama pada turn yang sama;
- retry dibatasi untuk kegagalan tertentu;
- auth Google dibedakan dari resource readiness;
- demo link harus berasal dari result tool, bukan dibuat model.

### 7.2 Yang belum OK

| Gap | Status | Risiko |
| --- | --- | --- |
| Tool call benar tetapi argumennya miskin konteks | `NOT OK` | Blueprint dan prompt generik. |
| Tidak ada semantic comparison antara discovery dan generated artifacts | `NOT OK` | Composer dapat menghilangkan atau mengubah requirement user. |
| Tidak ada behavioral test setelah create | `NOT OK` | Agent lolos verify tetapi gagal melayani customer. |
| Verify lebih banyak memeriksa resource/config daripada kualitas respons | `PARTIAL` | Technical readiness dianggap sama dengan user readiness. |
| Recovery dapat memaksa progression | `PARTIAL` | Sistem lebih fokus menyelesaikan create daripada menilai apakah progression tepat. |
| Idempotency end-to-end perlu canary | `PARTIAL` | Retry berpotensi membuat resource/row/link ganda. |
| Error message masih dapat berasal dari guard generik | `PARTIAL` | Clarification tidak selalu menjelaskan field spesifik yang kurang. |

### 7.3 Acceptance criteria tool orchestration

1. Semua material side effect memiliki idempotency key.
2. Setiap tool result dicatat dengan `success`, `resource_id`, `verified_at`, dan failure code.
3. Artifact composer harus menghasilkan requirement coverage map:
   - requirement user;
   - lokasi requirement pada blueprint/manual/instructions;
   - status `covered`, `missing`, atau `contradicted`.
4. `verify_agent` harus menjalankan minimal tiga simulation turn sesuai use case.
5. Agent tidak boleh ditawarkan ke demo bila simulation gagal.
6. Google integration tidak `ready` sebelum write-read verification dan binding agent lulus.
7. Demo message hanya boleh memakai link dan kode dari tool result.
8. Dedicated QR hanya boleh dikirim untuk agent dan device milik owner yang tepat.

## 8. QA kualitas agent hasil Arthur

### 8.1 Yang sudah OK

- Composer dipisah menjadi blueprint, operating manual, instructions, soul, dan validation.
- Instruction writer meminta prompt spesifik dan kontekstual.
- Instruction writer meminta contoh percakapan.
- Operating manual menyimpan workflow, state, approval, escalation, dan definition of done.
- Create tool menolak operating manual yang mengandung asumsi terdeteksi untuk agent bisnis.
- Placeholder dan nama bisnis yang tidak terverifikasi memiliki sanitasi.

### 8.2 Yang belum OK

Composer yang baik tidak dapat menggantikan discovery yang lemah. Pada Minsel, Arthur belum memperoleh:

- nama dan jenis bisnis;
- produk atau layanan;
- katalog, harga, stok, dan sumber kebenaran;
- intent customer utama;
- alur pemesanan;
- format data pembelian;
- aturan membaca dan memverifikasi bukti transfer;
- respons sebelum dan setelah eskalasi;
- contoh percakapan yang disetujui;
- tone dan panjang respons;
- definition of done.

Minsel dapat lolos secara struktur, tetapi belum mempunyai bahan untuk menjadi CS operasional.

### 8.3 Behavioral acceptance test untuk agent hasil creation

Setelah `verify_agent`, Arthur harus menjalankan QA simulasi:

| Scenario | Expected behavior |
| --- | --- |
| Customer menyapa tanpa konteks | Agent menyapa, memperkenalkan peran, dan menanyakan kebutuhan secara singkat. |
| Customer menanyakan produk | Agent memakai source resmi atau mengaku belum memiliki sumber. |
| Customer menanyakan stok/harga | Agent tidak mengarang dan menyebut langkah pengecekan. |
| Customer ingin membeli | Agent mengumpulkan field order yang sudah disetujui. |
| Customer mengirim gambar | Agent melakukan hanya tindakan media yang diizinkan. |
| Customer mengirim bukti transfer | Agent tidak menyatakan pembayaran sah tanpa authority/tool yang sesuai. |
| Customer meminta diskon | Agent menolak sesuai boundary tanpa terdengar kasar. |
| Customer meminta refund | Agent tidak menyetujui; menjalankan handoff. |
| Agent tidak tahu | Agent memberi acknowledgment, menjelaskan handoff, mengirim summary dan attachment ke operator. |
| Google Sheets gagal | Agent tidak mengklaim data tercatat dan memberi recovery yang benar. |

Agent baru dinilai `READY_FOR_DEMO` jika seluruh scenario kritis lulus.

## 9. QA Google Workspace

### 9.1 Yang sudah OK

- requirement Google dapat mengaktifkan mixin skill;
- auth link menggunakan tool resmi;
- produk Google dibatasi sesuai requirement;
- OAuth tidak dianggap bukti Sheet sudah ada;
- resource state dapat menyimpan ID/URL dan verification;
- workflow targetnya create/select Sheet, header, test write, read back, dan binding.

### 9.2 Yang belum OK

- Arthur belum menggali schema Sheet dari user;
- belum ada bukti pada log Minsel bahwa Sheet dan kolom pembelian sudah dibuat;
- “catat data pembelian” terlalu ambigu untuk menentukan trigger dan kolom;
- belum ada UX yang menjelaskan perbedaan login Google, Sheet siap, dan agent siap;
- perlu QA untuk duplicate append, retry, header mismatch, Sheet dipindah/dihapus, dan OAuth expired.

### 9.3 Release gate Google

Status harus dipisah:

1. `auth_required`;
2. `authenticated`;
3. `resource_required`;
4. `resource_created_or_selected`;
5. `schema_verified`;
6. `write_verified`;
7. `bound_to_agent`;
8. `ready`.

Arthur tidak boleh memakai kalimat “siap” bila status belum mencapai `ready`.

## 10. QA WhatsApp demo dan nomor khusus

### 10.1 Yang sudah OK

- Arthur menawarkan demo dan nomor khusus;
- pilihan `1`/`satu` dapat dipetakan ke demo;
- pilihan `2`/`dua` dapat dipetakan ke nomor khusus;
- link demo berasal dari `create_wa_dev_trial_link`;
- reply guard mengarahkan user dari dashboard ke flow WhatsApp yang benar;
- urutan link/kode dan contact delivery telah memiliki guard.

### 10.2 Yang belum OK

- pesan “agent belum siap launch” belum menjelaskan secara sederhana apa yang sudah siap dan apa yang belum;
- Google login dan pilihan channel muncul berdekatan tanpa status checklist;
- user dapat mengira login berarti Sheet dan agent sudah sepenuhnya siap;
- belum ada behavioral canary terhadap agent sebelum link demo diberikan;
- nomor khusus memerlukan QA device/session nyata dan isolation dari nomor demo.

## 11. Status automated test saat pemeriksaan

### 11.1 Lulus

Command:

```bash
PYTHONPATH=. pytest -q tests/test_arthur_fast_intake.py
```

Hasil:

```text
4 passed
```

Coverage suite ini:

- brief CS tidak diblokir optional questionnaire;
- pilihan demo bernomor menghasilkan action;
- greeting tidak memicu planning recovery;
- reasoning internal tidak dikirim saat greeting.

### 11.2 Belum lulus

Command:

```bash
PYTHONPATH=. pytest -q \
  tests/test_arthur_discovery_gate.py \
  tests/test_arthur_fast_intake.py
```

Hasil:

```text
45 passed, 3 failed
```

Kegagalan:

1. fixture Minsel dianggap belum lengkap karena hanya memiliki satu `ideal_conversations`;
2. test lama masih mengharapkan `go_live_approver` wajib, sedangkan kebijakan terbaru menjadikannya opsional;
3. evidence field opsional yang didelegasikan tidak lagi masuk `verified_evidence_fields`.

Kesimpulan QA:

- perubahan concise intake belum sinkron dengan seluruh test discovery;
- branch belum boleh dianggap green untuk area discovery;
- belum tersedia test conversational quality atau behavioral quality agent hasil creation.

## 12. Prioritas perbaikan

### P0 — sebelum menyatakan Arthur berkualitas

1. Tambahkan `business_readiness` gate terpisah dari schema completeness.
2. Buat adaptive probing berdasarkan use case dan side effect.
3. Tambahkan requirement coverage map dari discovery ke artifact.
4. Tambahkan behavioral simulation setelah create.
5. Sinkronkan dan hijaukan seluruh discovery tests.

### P1 — kualitas percakapan

1. Contextual acknowledgment.
2. Reframing sebelum masuk checklist.
3. Clarification hanya untuk bagian field yang hilang.
4. Summary membedakan fakta, inferensi, dan default.
5. Rubric naturalness, clarity, empathy, jargon, dan message length.

### P1 — orkestrasi

1. Audit log routing skill dan tool per turn.
2. Confidence/ambiguity policy untuk skill selection.
3. Integration test seluruh state transition.
4. Canary Google dan WhatsApp channel.

### P2 — optimasi

1. Ukur jumlah turn sampai ready tanpa mengorbankan readiness score.
2. Kumpulkan replay conversation nyata sebagai regression corpus.
3. Evaluasi model writer dengan golden artifact dan behavioral scenarios.

## 13. Definition of Done Arthur

Arthur baru dapat dinilai `QA PASS` bila:

1. flow bicara natural dan dapat dipahami user nonteknis;
2. tidak mengulang pertanyaan yang sudah dijawab;
3. dapat menggali workflow bisnis, bukan hanya mengisi field;
4. tidak mengarang detail material;
5. routing skill sesuai intent dan state;
6. tool yang dipanggil sesuai skill dan memiliki argumen grounded;
7. side effect diverifikasi;
8. Google/resource/channel readiness dinyatakan secara jujur;
9. agent hasil creation lulus behavioral simulation;
10. seluruh targeted test hijau;
11. runtime produksi membuktikan engine, prompt, bundle skill, seed, dan commit yang sama dengan release candidate.

## 14. Verdict

Verdict saat ini:

- **Arthur sebagai workflow orchestrator:** `PARTIAL PASS`, mendekati `PASS`.
- **Arthur sebagai agent builder teknis:** `PARTIAL PASS`.
- **Arthur sebagai konsultan discovery:** `FAIL`.
- **Arthur sebagai penjamin kualitas agent hasil creation:** `FAIL`.
- **Arthur secara end-to-end untuk user production:** `NOT READY FOR QA SIGN-OFF`.

Masalah utamanya bukan ketiadaan skill atau tool. Arthur sudah memiliki skill dan tool yang cukup lengkap. Gap terbesar berada pada:

1. kualitas fakta sebelum tool dipanggil;
2. cara Arthur menggali fakta tersebut;
3. quality gate setelah agent dibuat.

## 15. QA tambahan — Reminder WhatsApp pada agent buatan Arthur

### Verdict

**Status end-to-end: `FAIL / belum terbukti berfungsi`.**

Implementasi penyimpanan dan worker reminder tersedia, dan unit test scheduler
WhatsApp saat ini lulus. Namun jalur Arthur → konfigurasi agent → pemanggilan
tool → job tersimpan → worker hidup → pesan WhatsApp terkirim belum memiliki
jaminan end-to-end. Ada beberapa gap yang dapat membuat agent menjawab seolah
memahami reminder tanpa pernah membuat job yang dapat dikirim.

### Yang sudah OK

| Area | Bukti | Status |
|---|---|---|
| Tool scheduler tersedia | `app/core/tools/scheduler_tool.py` dapat membuat, melihat, dan membatalkan reminder | PASS |
| Zona waktu relatif | Parser menggunakan zona waktu lokal UTC+7 | PASS |
| Job disimpan ke database | `ScheduledJob` dibuat dan di-commit sebelum tool memberi respons berhasil | PASS |
| Worker tidak lagi memproses backlog secara serial | Worker mengambil due jobs, memprioritaskan non-heartbeat, lalu menjalankannya sebagai task | PASS |
| Delivery memakai channel session | Worker memakai `channel_config`, `user_phone`, dan `device_id` sesi WhatsApp | PASS |
| Retry delivery | Kegagalan kirim one-time reminder dijadwalkan ulang satu menit kemudian | PASS |
| Test komponen | `PYTHONPATH=. pytest -q tests/test_scheduler_whatsapp.py` menghasilkan `6 passed` | PASS |

### Yang belum OK

| Gap | Dampak user | Status |
|---|---|---|
| Preset CS umum memiliki `tools_config.scheduler = false` | Agent CS buatan Arthur tidak otomatis mempunyai tool reminder | FAIL |
| Deteksi preset memprioritaskan ecommerce/CS sebelum scheduler | Requirement campuran “CS + reminder” dapat berakhir sebagai preset CS tanpa scheduler | FAIL |
| Override scheduler bergantung pada reminder masuk ke daftar `features` | Jika Arthur menangkap reminder hanya di goal/deskripsi umum, scheduler tidak aktif | FAIL |
| Instruksi tool salah kontrak | Agent diajari `set_reminder(message, run_at)`, sedangkan tool sebenarnya meminta `set_reminder(label, message, schedule)` | FAIL |
| Instruksi reminder hanya ditambahkan berdasarkan config preset | Agent CS yang scheduler-nya diaktifkan melalui override masih dapat tidak memperoleh petunjuk reminder | FAIL |
| Self-healing hanya per turn dan berbasis regex sempit | Ungkapan seperti “nanti jam 5 kabarin saya” dapat tidak memperoleh tool scheduler; aktivasi juga tidak disimpan ke config agent | PARTIAL |
| Health API hanya menulis `scheduler: external` | API dapat sehat walaupun container scheduler mati; nilai ini bukan hasil liveness check | FAIL |
| Service scheduler production tidak memiliki healthcheck | Kondisi worker mati tidak terlihat dari health aplikasi utama | FAIL |
| Tidak ada test Arthur-created-agent end-to-end | Test sekarang tidak membuktikan agent memanggil tool, job terbentuk, worker mengambil job, dan WhatsApp menerima pesan | FAIL |

## 16. Root cause reminder WhatsApp

### Root cause utama 1 — kontrak instruksi berbeda dengan tool nyata

`app/core/tools/builder_instruction_tools.py:147` mengarahkan instruction
writer untuk menuliskan:

```text
set_reminder(message, run_at)
```

Sementara implementasi sebenarnya di
`app/core/tools/scheduler_tool.py:117` adalah:

```text
set_reminder(label, message, schedule)
```

Pembatalan juga diarahkan sebagai `cancel_reminder(id)`, sedangkan implementasi
di `app/core/tools/scheduler_tool.py:326` menerima `cancel_reminder(label)`.

Ini bukan sekadar perbedaan nama dokumentasi. Instruksi agent bertentangan
dengan schema tool yang tersedia saat runtime. Akibatnya model dapat:

1. mengirim argumen yang tidak lengkap;
2. memakai nama argumen yang tidak dikenal;
3. tidak memanggil tool karena instruksi dan tool schema tidak konsisten;
4. memberi konfirmasi natural-language walaupun job tidak pernah tersimpan.

### Root cause utama 2 — scheduler tidak otomatis ikut pada agent CS

Preset `cs_whatsapp_basic`, `ecommerce_cs`, dan sebagian besar preset bisnis
memiliki scheduler nonaktif. Hanya preset khusus seperti
`scheduler_assistant`, `social_media_agent`, dan `personal_assistant` yang
mengaktifkannya secara default.

Di `app/core/tools/builder_intent.py:112-121`, ecommerce dan CS diperiksa lebih
dulu daripada scheduler. Karena itu goal campuran lebih mungkin dipilih sebagai
agent CS. `builder_planning_tools.py:416` memang dapat mengaktifkan scheduler
melalui feature override, tetapi hanya jika Arthur secara eksplisit membawa
kata reminder/jadwal ke daftar features. Requirement yang tertinggal di goal
atau percakapan discovery tidak cukup menjamin aktivasi.

### Root cause utama 3 — fallback runtime terlalu sempit dan tidak persisten

`app/core/engine/agent_tool_setup.py:60-71` memiliki self-healing untuk sesi
WhatsApp ketika pesan mengandung salah satu kata yang cocok:

```text
reminder, remind, pengingat, ingatkan, ingetin, alarm,
follow-up, jadwalkan, jadwalin
```

Fallback ini membantu kasus eksplisit, tetapi:

1. tidak mencakup seluruh ungkapan natural user;
2. hanya mengaktifkan tool untuk turn tersebut;
3. tidak memperbarui `tools_config.scheduler` agent;
4. tidak menjamin model benar-benar memanggil tool.

Dengan demikian self-healing menutupi sebagian gejala, tetapi tidak memperbaiki
hasil provisioning Arthur.

### Root cause operasional yang belum dapat dieliminasi — worker production

Endpoint production `/health/detailed` menampilkan scheduler sebagai
`external`. Di `app/main.py:303`, nilai itu ditulis ketika embedded scheduler
dimatikan; nilainya tidak mengecek proses scheduler eksternal.

`deploy/docker-compose.prod.yml:74-79` memang mendefinisikan service scheduler
dengan `python -m app.scheduler_worker`, tetapi service tersebut tidak memiliki
healthcheck. Akses read-only ke container/log production belum tersedia pada
pemeriksaan ini, sehingga status worker aktual belum dapat dibuktikan.

Artinya ada dua kemungkinan yang harus dibedakan:

1. **tidak ada row `scheduled_jobs`** → masalah provisioning/tool-call;
2. **row ada tetapi tidak terkirim** → masalah worker, session/channel config,
   atau WhatsApp delivery.

Health API sekarang tidak dapat membedakan kedua kondisi tersebut.

## 17. Coverage test yang masih hilang

Test yang ada membuktikan helper dan worker secara terpisah, tetapi belum
menguji skenario user nyata berikut:

1. Arthur membuat agent CS dengan requirement reminder;
2. hasil `tools_config.scheduler` harus `true`;
3. instruction hasil creation menyebut signature tool yang benar;
4. customer mengirim “tolong kabarin saya nanti jam 5”;
5. agent wajib memanggil `set_reminder`, bukan hanya menjawab;
6. row `scheduled_jobs` wajib terbentuk dengan `agent_id`, `session_id`,
   `next_run_at`, dan payload yang benar;
7. worker eksternal mengambil row tersebut;
8. pesan dikirim memakai device/session WhatsApp yang benar;
9. status job menjadi `done` atau memiliki delivery failure yang observable.

Sebelum coverage ini tersedia, kelulusan enam unit test scheduler tidak boleh
dipakai sebagai bukti bahwa reminder agent buatan Arthur sudah berfungsi.

## 18. Urutan perbaikan yang direkomendasikan

### P0

1. Samakan kontrak instruction writer dengan signature tool sebenarnya.
2. Validasi requirement coverage: bila discovery menyebut reminder, final
   `tools_config.scheduler` wajib `true`.
3. Larang agent mengonfirmasi reminder sebelum tool sukses dan job ID/label
   diterima.
4. Tambahkan liveness/readiness check untuk scheduler worker production.

### P1

1. Buat deteksi reminder berbasis intent yang lebih luas daripada regex keyword.
2. Persist self-healing atau tandai agent configuration drift untuk diperbaiki.
3. Tambahkan integration test creation sampai database job.
4. Tambahkan canary reminder WhatsApp untuk membuktikan delivery production.

### Kesimpulan

Penyebab paling langsung yang sudah terbukti dari source adalah **Arthur dapat
membuat agent CS tanpa scheduler aktif dan instruction writer memberi kontrak
tool reminder yang salah**. Setelah dua hal itu diperbaiki, worker production
tetap harus diverifikasi karena health API saat ini hanya menyatakan mode
`external`, bukan memastikan proses pengirim reminder hidup.

## 19. Status implementasi setelah QA

Perbaikan source untuk jalur reminder sudah diterapkan setelah temuan di atas:

1. kontrak instruction writer sekarang sesuai dengan signature runtime
   `set_reminder(label, message, schedule)` dan `cancel_reminder(label)`;
2. agent dilarang mengklaim reminder berhasil sebelum ada hasil tool yang sukses;
3. requirement reminder pada discovery agent CS mengaktifkan
   `tools_config.scheduler` tanpa mengubah preset utama menjadi scheduler;
4. informasi jadwal biasa, seperti pertanyaan jadwal kelas, tidak dianggap
   sebagai permintaan membuat reminder;
5. runtime self-healing memahami ungkapan natural seperti
   “nanti jam 5 kabarin saya”;
6. runtime contract membantu agent lama memakai signature tool yang benar;
7. test membuktikan pemanggilan tool membuat row aktif di `scheduled_jobs`;
8. worker eksternal menerbitkan heartbeat Redis, `/health/detailed` menjadi
   degraded ketika heartbeat hilang, dan service scheduler mendapat Docker
   healthcheck.

Validasi setelah implementasi:

- 86 focused tests pada planning, instructions, scheduler runtime, reply guard,
  database job, dan health check lulus;
- production Compose tervalidasi;
- full suite: 1.140 passed, 9 skipped, dan 13 failure lama di luar scope
  reminder.

Status ini baru membuktikan source dan regression test. Deployment serta canary
pengiriman reminder melalui nomor WhatsApp production masih diperlukan sebelum
status user-facing dapat dinaikkan menjadi `PASS`.
