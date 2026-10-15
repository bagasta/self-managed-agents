# Pemeriksaan link Google pertama

Tanggal: 14 Oktober 2026. Status: lokasi kegagalan dipersempit; akar penyebab browser/provider belum terkonfirmasi. Tidak melakukan deploy atau perubahan konfigurasi.

Konfirmasi pengguna: error muncul di accounts.google.com; link awal dan pengganti sama-sama dibuka langsung dari WhatsApp pada HP yang sama. Karena itu, pergantian browser secara sengaja bukan penjelasan yang didukung bukti.

## Temuan yang terbukti

- Pesan yang diteruskan pengguna ke Arthur pada 16:27 WIB berbunyi: `400. That’s an error.` dan menyatakan request malformed. Ini berbeda dari laporan 404.
- Link pertama yang tersimpan pada jawaban Arthur sama persis dengan auth_url hasil create_assistant. Link pengganti juga sama dengan hasil start_assistant_google_oauth. Host, path, panjang token, dan fingerprint cocok; token/link aktif tidak dicantumkan di laporan ini.
- Endpoint pembuka integrasi mencatat redirect HTTP 302 untuk kedua link pertama, tanpa HTTP 404 pada endpoint tersebut dalam log hari ini yang diperiksa. Token awal belum kedaluwarsa ketika dibuka.
- Percobaan 15:43 WIB: link awal dibuka pada 15:45 WIB, dua kali HTTP 302, tanpa callback. Link baru dibuat 15:47 WIB; callback selesai HTTP 303 pada 15:47:38 WIB.
- Percobaan 16:25 WIB: link awal dibuka pada 16:26 WIB, dua kali HTTP 302, tanpa callback. Setelah error 400 diteruskan ke Arthur, link baru dibuat 16:27 WIB dan callback selesai HTTP 303 pada 16:28:16 WIB.
- State awal kedua percobaan masih ada di database integrasi. Scope-nya sama dengan koneksi yang akhirnya berhasil: identitas Google dan Google Sheets. PKCE verifier 86 karakter dan challenge 43 karakter; callback URL sama dan benar.
- Tujuan redirect setelah OAuth, chiefaiofficer.id/app?oauth_success=1, mengembalikan HTTP 200 ketika diperiksa. Route callback integrasi ada; request tanpa parameter mengembalikan HTTP 400 sebagaimana diharapkan.
- Dua request diagnostik baru langsung ke Google, dengan client/callback/scopes produksi serta state/PKCE acak tanpa menyimpan OAuth state pengguna, sama-sama mengembalikan HTTP 302 menuju accounts.google.com/v3/signin/identifier. Tidak muncul error malformed.
- Uji tambahan memakai User-Agent Chrome Android: endpoint authorization HTTP 302, halaman login berikutnya HTTP 200. Mengikuti redirect dengan sesi cookie yang sama maupun sesi cookie baru sama-sama menghasilkan halaman Sign in Google tanpa error malformed. Uji ini tidak memakai akun pengguna atau melakukan consent.

## Kesimpulan dan batas bukti

Pada percobaan yang cocok, pembuatan link Arthur dan redirect awal layanan integrasi berjalan. Login pertama berhenti sebelum callback mencapai layanan integrasi; callback baru terjadi pada percobaan pengganti. Tidak ada bukti bahwa link pertama salah token, expired, berbeda scope, atau route start mengembalikan 404.

Pesan error menunjukkan kegagalan request di halaman Google, tetapi log server tidak merekam halaman maupun state browser pengguna di sana. Karena itu belum dapat dipastikan apakah penyebabnya navigasi browser dalam WhatsApp, state sesi Google, atau faktor lain. Kemungkinan tersebut belum merupakan diagnosis dan tidak dijadikan dasar perubahan produksi.

Informasi lanjutan yang diminta: domain/alamat halaman saat error muncul—google-workspace-mcp.chiefaiofficer.id, accounts.google.com, atau domain lain. URL lengkap dapat mengandung state/token; jangan menyalinnya ke laporan publik.

Domain sudah dikonfirmasi: accounts.google.com. Bukti yang masih diperlukan untuk diagnosis spesifik adalah screenshot halaman error dengan alamat/path dan kode/detail error Google yang terlihat. Domain dan pesan malformed generik saja tidak membedakan parameter yang ditolak dari masalah sesi/navigasi di pihak browser/provider. Tidak ada dasar untuk mengklaim cookie, WhatsApp preview, atau PKCE sebagai akar masalah.

Menurut [referensi resmi Google OpenID Connect](https://developers.google.com/identity/openid-connect/reference), invalid_request HTTP 400 mencakup parameter yang hilang/tidak valid atau request malformed; penolakan embedded user-agent mempunyai kategori tersendiri, disallowed_useragent. Pesan yang tersedia belum menunjukkan kategori spesifik tersebut.

## Tindakan selama investigasi

Hanya membaca log, konfigurasi runtime, state OAuth, dan pesan yang relevan, serta menjalankan pemeriksaan HTTP tanpa login/consent atau perubahan data. Tidak membuka link aktif milik pengguna, mengubah koneksi, mereset token, mengirim pesan, atau merestart layanan. Perubahan lokal yang sudah ada pada repository integrasi dibiarkan utuh.
