# QQ channel for the existing persona

This is a channel-only Cordis plugin running in the project's pinned official
DeepSeek Harness `sdk-minimal` runtime. It never creates an agent/session or
calls a model. Python remains the authority for pairing, conversation state,
private memory, model selection, durable deduplication and send receipts.

Dependencies are pinned in `package-lock.json`:

- `@tencent-connect/qqbot-nodejs` **1.0.5**, Tencent's official QQ Open Platform SDK.
- `@tencent-connect/qqbot-connector` **1.2.0**, the same QR connector used by Tencent's dsh-qqbot plugin.
- DSH **0.2.1-rc.2**, resolved from the sibling `tools/deepseek-harness` installation.

Install from the project root:

```powershell
npm.cmd ci --prefix tools/deepseek-harness --ignore-scripts --no-audit --no-fund
npm.cmd ci --prefix tools/qq-harness --ignore-scripts --no-audit --no-fund
```

The host launches `node tools/qq-harness/runner.mjs`, then sends one JSONL
`start` object with `endpoint`, `token`, `home` and `workspace` absolute paths.
`home` and `workspace` must be dedicated persistent QQ runtime directories.
`stop`, `cancel` or stdin EOF shuts down the SDK-owned subprocess. Stdout emits
only `{"type":"ready"}` or fixed error codes. Raw SDK logs/errors are not forwarded.
Only OS process variables and `VTUBER_QQ_BRIDGE` / `VTUBER_QQ_TOKEN` reach DSH;
provider keys and QQ credentials never appear in environment variables or patches.

The bridge must bind to `http://127.0.0.1:<port>` and authenticate the per-run
bearer token. JSON endpoints return `{ok:true,...}` on success:

- `GET /commands`: `{command:null|{id,type:"bind"|"connect"|"disconnect",credentials?:{appId,appSecret}}}`.
- `POST /events`: `qr` with `url`; `bound` with `app_id,app_secret`; `connected` with `app_id`; `disconnected`; or `error` with a fixed `code`.
- `POST /message` accepts private messages as `{app_id,peer_id,message_id,text,nickname?}` (the existing form remains valid), or mentioned group messages as `{kind:"group",app_id,group_id,peer_id,message_id,text,mentioned:true,nickname?}`. It returns `{reply?,receipt_id?,reply_mode?:"text"|"voice"}`. No reply means no send; omitted mode means text. Group IDs and sender IDs are separate, and only an SDK-confirmed bot mention may set `mentioned:true`.
- `GET /audio/<receipt_id>`: authenticated, uncached private WAV bytes for a pending voice reply only, at most 3,500,000 bytes. This is never a public URL or caller-selected file path.
- `POST /receipt`: `{receipt_id,status:"sent"|"unknown"|"failed"}`.

There is no automatic QR creation or connection. The host must explicitly issue
the corresponding command. After `bound`, the host saves credentials before it
issues `connect`. QR links are limited to
`https://q.qq.com/qqbot/openclaw/connect.html?task_id=...`; no external QR renderer
or third-party image request is needed.

Only C2C text and group text explicitly mentioning the bot are forwarded; ordinary
unmentioned group messages, guild/DM events, bot echoes, audio input and missing
reply identities are ignored. The host decides which private peer is the paired
owner; group members do not need owner pairing, and group messages cannot perform
owner pairing. Group conversations are isolated by group and member and do not
read owner memory or private history. Python caches at most 32 group sessions in
memory and retains at most 512 group sessions on disk. Replies
are single text messages (maximum 4,000 Unicode code points, matching Python) or voice messages linked to
the original incoming message, never proactive sends. Per-conversation queues serialize
messages, with 8 pending per conversation and 32 total. Private and group-member
queue keys are distinct. A duplicate in the current process
is dropped; restart-safe deduplication belongs to Python.

Before returning a reply, Python must durably record its sending barrier. The
pinned SDK's `sendText` path performs one API POST without retry. Network failure,
5xx, or an invalid success receipt is recorded as `unknown`; it is never retried.
Failure to save a receipt also never resends. Disconnect cancels queued work;
an already-started remote send may finish and must be reconciled by its receipt.

The host's persisted `reply_mode` applies to both private and group replies. Its
local management status exposes `group_enabled` and `group_session_count`; no
separate group activation command or owner pairing is needed. It retains the
60-second and 3,500,000-byte voice limits for both conversation types.

Voice uses WAV `file_data` with `file_name: "reply.wav"` and `srv_send_msg:false`,
then sends the returned `file_info` as a media message only while the connection
is still current. The byte limit accounts for base64 within the platform's 5 MB
direct-upload limit. A failed upload does not send; a final send with an unknown
outcome never falls back to text. The host keeps canonical reply text for history,
committing it only after the voice message's successful send receipt. Synthesis
failure/length fallback belongs to the host and happens before any remote send.

Offline checks from the project root:

```powershell
node tests/test_qq_harness_transport.mjs
node tests/test_qq_harness_sdk.mjs
node tests/test_qq_harness_runtime.mjs
```

The last check starts the real DSH runtime and imports the real QQ plugin, with
only a local fake command server. It does not bind an account, open a QQ gateway,
send messages, or call a model.
