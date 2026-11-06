# Bilibili DeepSeek Harness carrier

This directory runs the **official DeepSeek Harness**, independently of the
project's OMP/Astra integration. Its model tools save drafts and interaction
proposals; publication, likes and comments stay in the local application's
host-side workflow under the saved account, switches and daily limits.

The application binds this executor to the **currently instantiated Persona**.
The Harness's custom `vtuber-persona` provider asks the Python host for each
reading decision. The existing Persona chooses its configured provider/backend;
the Harness child has no model credential and makes no direct DeepSeek request.
The official Harness still owns the agent loop and executes the six host tools.

The reading, interaction and diary capabilities draw on the ideas and interface
contracts of `dsh-bili-scroll`, `dsh-bili-interact` and `dsh-bili-whale`. They are
adapted to this application's existing Persona, scheduler and browser bridge;
the three upstream packages and their independent persona/scheduling systems
are not installed wholesale.

## Install and verify

Requires Node.js `^22.19.0 || >=24.0.0` and the project's Python environment.
Install locally from this directory (never globally):

```powershell
npm ci --ignore-scripts --no-audit --no-fund
```

From the repository root, this checks actual SDK import, tool registration and
an idle-session initialization handshake **without credentials or model input**:

```powershell
.venv\Scripts\python.exe -X utf8 tools/deepseek-harness/verify.py
.venv\Scripts\python.exe -X utf8 -m unittest tests.test_deepseek_harness -v
node tests/test_persona_harness_plugin.mjs
node tools/deepseek-harness/verify-persona.mjs
.venv\Scripts\python.exe -X utf8 tools/deepseek-harness/verify-persona.py
```

`verify-persona.mjs` runs the actual SDK loop against a deterministic localhost
fake: seven model decisions invoke search, read, note, draft, diary and
interaction once each, then finish. The standard DeepSeek provider is disabled and no provider keys are
passed to the child. This verifies the real loop without an external model call.
`verify-persona.py` extends this through the actual Python model bridge and
Bilibili activity service, saving a note, dynamic draft, public diary and unsent
interaction proposal against local fixtures. Both checks cover 7 decisions and
6 tool calls without a real model, browser account or submission.

Dependencies are pinned to `0.2.1-rc.2`; the lock file pins their full dependency
tree. SDK contracts were checked against upstream commit
`687f282e8625c6b8d11ff58211a90524be2e4317`:

- [TypeScript SDK API](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/sdk/client/src/api.ts)
- [SDK option types](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/sdk/client/src/types.ts)
- [sdk-minimal composition](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/bundle/sdk-minimal/cordis.patch.yml)
- [Official tool plugin API](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/docs/cookbook/adding-a-tool.md)
- [Official provider adapter interface](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/llm/llm/src/index.ts)

The Python `HarnessRunner.readiness()` checks installed package versions, Node
availability, and the selected model binding. It never calls a model.
`run(context, call_tool)` is the model-running entry point. Do not use a live
model-backed `run()` as an installation check.

## Host contract

The application supplies the live Persona binding. Its model request stays in
Python, uses the existing backend, and produces one validated decision per step.

```python
runner = HarnessRunner.from_persona(root, persona)
text = await runner.run(context, call_tool)
```

`persona-provider.mjs` registers `vtuber-persona` through the official
`ctx.llm.registerAdapter()` seam and converts each decision into native Harness
text or tool-call chunks. This also supports text-only Persona backends: native
function calling is not required at the external provider.

For the Persona binding, the profile disables `llm-deepseek`, inserts
`persona-provider.mjs` as `persona-provider`, and adds `personaProvider` to the
SDK initialization dependencies. The start message names provider
`vtuber-persona` and the current Persona's model name. `POST /model` sends only
`{messages: [...]}` with public text/tool history, never internal source/replay
state or reasoning. The authenticated host replies with either
`{ok:true,result:{type:'tool',name,arguments}}` or
`{ok:true,result:{type:'finish',text}}`; the adapter validates the reply and
forwards cancellation to the HTTP request.

The older explicit `HarnessRunner(model="deepseek-v4-flash")` API is retained
for compatibility. It uses the official DeepSeek provider and its configured
credential. It is not the application's Persona-bound path.

`call_tool(name, arguments)` is asynchronous and returns a JSON dictionary.
Expected business rejections should return `{ "ok": false, "error": "reason" }`.
Unexpected exceptions are converted to fixed error categories; raw provider or
exception strings are never exposed to the model or operator UI. Only these
six tools exist:

- `bili_search(query)`
- `bili_read(url)`
- `bili_note(source_url, summary, reaction)`
- `bili_draft(text, source_urls)`
- `bili_diary(text, source_urls)`
- `bili_interaction(action, url, text)`

Source URLs must be HTTPS on the supported Bilibili hosts. The host requires
reads to come from this round's search results, and drafts/interactions to come
from this round's actual reads. A diary may reference only public notes saved
during this round, with at most 1,200 characters of final diary text. Each round
may save at most one dynamic draft, one diary and one interaction proposal.
Tool output and persona context are data, not permission to add tools or execute
commands. Activity input contains the character and public reading context, not
private chat history.

`bili_interaction` accepts only `like` or `comment`; `text` must be empty for a
like and contain the final comment of at most 500 characters for a comment. The
tool saves a `draft` proposal and returns `sent: false`. The host may execute it
after the round while idle only if the corresponding saved `allow_likes` or
`allow_comments` switch permits it. Both switches default to false. Manual
execution follows the same switches, expected UID and quota. Daily interactions
default to 3, accept 0–10, and count unknown attempts; 0 prevents execution.
Activity hours and quota days use UTC+8. The browser verifies identity and
persists a duplicate-submission barrier before clicking. An `unknown` result
can only be verified, never automatically sent again.

Browser extension **1.2.0** provides the reader and interaction commands. Its
reader supplements visible page evidence through fixed read-only video,
subtitle and hot-comment APIs using the normal browser session, without reading
or exporting Cookie values. Only the first video part and one available subtitle
track are read, capped at 12,000 characters / 2,000 cues. `full_subtitles` means
the obtained track is complete within that part; `subtitle_excerpt` marks
truncation. `visible_subtitles` and `page_metadata` are limited fallbacks, never
evidence of watching the complete video. Up to 10 hot comments are other users'
opinions, not video facts. No video-frame understanding, favorites, follows or
private-message listener is included.

Notes and public diaries stay in the host's bounded local store. The existing
web/voice and QQ conversation paths recall relevant entries by topic or date,
up to 3 entries / 3,500 characters; group chat requires explicit `public`
visibility. Saving a diary does not publish it or change the character card.
The activity UI exposes source evidence, diary entries, proposals and receipts.
Full account, publication and recovery behavior is documented in
[`docs/bilibili-activity.md`](../../docs/bilibili-activity.md).

## Runtime and limits

The Python host starts `runner.mjs` with one JSONL `start` message containing
the context and temporary paths. The carrier returns one `final` message or a
fixed `error` category. A `cancel` message closes the SDK and its runtime.
The Persona-bound child receives only the random bridge token, with no model
credentials. The older explicit DeepSeek mode carries its own credential only
in the child environment.

The official SDK starts a fresh `sdk-minimal` profile with our patch. That
upstream profile normally includes terminal and filesystem tools, so the patch
explicitly disables them, MCP resources, compaction, and retry plugins. A
required custom plugin restricts every agent's tool schemas to the six names,
checks the resulting surface, and denies any other execution. Initialization
depends on this plugin's guard service, so missing guards fail closed.

The plugin contacts a token-protected loopback HTTP bridge to admit each model
step before it runs and to execute permitted host tools. Model steps, tool calls,
total duration, argument sizes and result sizes are bounded. Timeout/cancellation
stops callbacks and the owned child process tree. A temporary Harness home
isolates session data from project/user Harness settings and is removed at exit.

No real model, login, publishing, or hardware operation is used by the offline
Python tests. Windows sandbox environments may require normal local process
permissions for asyncio's named subprocess pipes.
