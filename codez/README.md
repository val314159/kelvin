# Codex Web: one-file mobile-first Codex CLI wrapper

## Purpose

Build a small, single-file Python web app that gives a mobile-first browser chat interface to the Codex CLI.

The web UI should feel like an interactive chat, but the backend should be turn-based:

1. The browser sends one user message over a WebSocket.
2. The server runs one `codex exec --json ...` command for that turn.
3. The server streams Codex JSONL events back to the browser.
4. The Codex process exits at the end of the turn.
5. The next browser message resumes the prior Codex thread with `codex exec resume --last ...`.

Do **not** build a terminal emulator. Do **not** wrap the interactive Codex TUI. Do **not** use Codex `app-server` mode for this version.

This is a compact v0 tool, not VS Code in a trench coat.

---

## Hard requirements

Implement everything in one Python file, for example:

```text
codez.py
```

Use:

```text
Bottle
gevent
gevent-websocket
docopt
standard library
```

The file may contain the HTML, CSS, and JavaScript as multiline strings.

The JavaScript may use pinned CDN libraries for Markdown rendering, sanitization, and syntax highlighting.

The server should default to running in the current working directory, and the working directory **must contain a `.git` directory**. If `.git` is missing, fail fast with a clear error.

---

## Python dependencies

The README implementer should assume this install command:

```bash
pip install bottle gevent gevent-websocket docopt
```

External CLI dependency:

```bash
codex
```

The user is expected to already have Codex CLI installed and authenticated.

---

## Command-line interface

Use `docopt`.

Suggested usage string:

```text
Usage:
  codez.py [--host=<host>] [--port=<port>] [--root=<dir>] [--user=<user>] [--password=<password>] [--secret=<secret>]
  codez.py -h | --help

Options:
  --host=<host>          Bind host [default: 127.0.0.1]
  --port=<port>          Bind port [default: 8080]
  --root=<dir>           Workspace root [default: .]
  --user=<user>          Login user [default: val]
  --password=<password>  Login password [default: change-me]
  --secret=<secret>      Cookie signing secret [default: change-this-cookie-secret]
  -h --help              Show help.
```

Normalize `--root` to an absolute real path.

Require:

```text
<root>/.git exists
```

If not, print an error and exit nonzero.

---

## Auth

Use a deliberately simple auth model for v0:

```text
hardcoded/default username
hardcoded/default password
signed cookie
```

Routes:

```text
GET  /login   -> login form
POST /login   -> validate credentials and set signed cookie
GET  /logout  -> clear cookie and redirect to /login
GET  /        -> app page if authenticated, else redirect to /login
```

Cookie requirements:

```text
signed Bottle cookie
HttpOnly
SameSite=Lax
path=/
```

Use Bottle’s signed cookie helpers if practical.

The WebSocket route must also check the signed cookie before accepting useful work. If auth fails, close or send an auth error and close.

Do not build users, registration, password reset, OAuth, or anything fancy.

Security note for README/user-facing docs:

```text
This is suitable for localhost/private use. Do not expose it raw to the public internet. If exposed remotely, put it behind HTTPS and another authentication layer.
```

---

## Server architecture

Use Bottle with gevent monkey patching.

The app should have one global active Codex job at a time.

Global-ish state:

```python
ROOT = ...
USER = ...
PASSWORD = ...
SECRET = ...
ACTIVE = None      # current subprocess object or job object
FIRST_TURN = True  # first turn uses `codex exec`; later turns use `codex exec resume --last`
SCROLLBACK = []    # optional in-memory browser transcript/events
```

It is acceptable for v0 to support one active browser/chat session per server. Multiple browsers may connect, but only one active Codex run should exist globally.

If a user sends a new prompt while Codex is already running, return a structured error/status message:

```json
{"type":"error","text":"Codex is already running"}
```

---

## Codex invocation

Use JSONL output.

First turn:

```bash
codex exec --json --sandbox workspace-write --ask-for-approval never --cd "$ROOT" -
```

Later turns:

```bash
codex exec resume --last --json --sandbox workspace-write --ask-for-approval never --cd "$ROOT" -
```

Important details:

- Pass the user prompt on stdin.
- Do not use `--ephemeral`; resume needs Codex’s local session history.
- Use `--cd "$ROOT"` even when the Python server is already running from that directory.
- Do not use `--full-auto` in this v0. We intentionally choose explicit `workspace-write + approval never`.
- Do not use `--dangerously-bypass-approvals-and-sandbox`.
- Do not use `--yolo`.
- Do not invoke the interactive `codex` TUI.

Reasoning:

- The browser is interactive, but Codex runs one non-interactive turn at a time.
- `--json` gives structured JSONL events suitable for a browser UI.
- `workspace-write` lets Codex edit the repo/workspace.
- `--ask-for-approval never` prevents a non-interactive web run from getting stuck on an approval prompt.

---

## Process control

Run Codex as a subprocess.

Preferred:

```python
subprocess.Popen(
    cmd,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    bufsize=1,
    cwd=ROOT,
    start_new_session=True,
)
```

Use `start_new_session=True` so the Stop button can terminate the whole process group.

Stop behavior:

1. Send SIGINT to the process group.
2. Wait briefly.
3. If still running, send SIGTERM to the process group.
4. Wait briefly.
5. If still running, send SIGKILL to the process group.

Use standard library:

```python
os.killpg(proc.pid, signal.SIGINT)
os.killpg(proc.pid, signal.SIGTERM)
os.killpg(proc.pid, signal.SIGKILL)
```

Be careful on platforms where process groups behave differently. Linux/macOS are the expected platforms.

---

## WebSocket protocol

Use WebSocket for chat input, Codex output, stop, diff, and file viewer actions.

Endpoint:

```text
GET /ws
```

Browser-to-server messages should be JSON:

```json
{"type":"prompt","text":"Fix the failing parser test."}
{"type":"stop"}
{"type":"diff"}
{"type":"files"}
{"type":"file","path":"README.md"}
{"type":"clear"}
```

Server-to-browser messages should also be JSON:

```json
{"type":"status","state":"idle"}
{"type":"status","state":"running"}
{"type":"status","state":"done","exit":0}
{"type":"status","state":"stopped"}
{"type":"chat","role":"user","text":"Fix the failing parser test."}
{"type":"chat","role":"assistant","text":"I updated parser.py and added tests."}
{"type":"terminal","text":"$ pytest\n..."}
{"type":"codex_event","event":{...}}
{"type":"diff","text":"diff --git ..."}
{"type":"files","files":[{"path":"README.md","size":1234},{"path":"src/app.py","size":4567}]}
{"type":"file","path":"README.md","text":"# Project","mode":"markdown"}
{"type":"error","text":"Something went wrong"}
```

Keep the protocol forgiving. Unknown client message types should produce an error instead of crashing. Unknown Codex event types should be forwarded as debug/structured events or ignored safely.

---

## Parsing Codex JSONL

Codex stdout in `--json` mode is JSON Lines: one JSON object per line.

For each stdout line:

1. Strip newline.
2. If empty, ignore.
3. Try `json.loads`.
4. If parsing fails, forward the raw line as terminal/debug text.
5. If parsing succeeds, route it by event type.

Useful event handling:

```python
event_type = event.get("type")
item = event.get("item") or {}
item_type = item.get("type")
```

Handle at least:

```text
thread.started
turn.started
turn.completed
turn.failed
item.started
item.completed
error
```

Item types to recognize:

```text
agent_message
command_execution
file_change
reasoning
plan_update
web_search
mcp_tool_call
```

Rendering rules:

- `item.completed` + `item.type == "agent_message"`:
  - send `{"type":"chat","role":"assistant","text": item["text"]}`
- `item.completed` + `item.type == "command_execution"`:
  - send `{"type":"terminal","text": ...}`
  - include command and output when present
- `item.completed` + `item.type == "file_change"`:
  - send as terminal/status/debug text; do not assume schema stability
- `turn.completed`:
  - send `{"type":"status","state":"done"}`
  - set `FIRST_TURN = False`
- `turn.failed` or `error`:
  - send an error/status message
- unknown events:
  - optionally send `{"type":"codex_event","event": event}`

Do not overfit the Codex schema. It may evolve.

Also read stderr in a greenlet and forward lines as:

```json
{"type":"terminal","stream":"stderr","text":"..."}
```

The app should not freeze just because Codex writes progress or warnings to stderr.

---

## Browser UI

Mobile first.

One HTML page, served from `/`.

The target feel is:

```text
ChatGPT-style mobile chat
+ collapsible command/terminal output
+ git diff panel
+ read-only GitHub-ish file viewer
```

It should not feel like a terminal emulator, IDE, or desktop app squeezed onto a phone. It should feel like a clean mobile control panel for Codex.

### Visual structure

Use a single-column layout on phones:

```text
┌────────────────────────────────────┐
│ Codex Web          repo: my-project │
│ ● idle                         logout│
├────────────────────────────────────┤
│                                    │
│ You                                │
│ Fix the failing parser test.       │
│                                    │
│ Codex                              │
│ I found the parser issue in        │
│ src/parser.py and updated the      │
│ token handling.                    │
│                                    │
│ ▸ Command output                   │
│ ┌────────────────────────────────┐ │
│ │ $ pytest                       │ │
│ │ 12 passed in 0.82s             │ │
│ └────────────────────────────────┘ │
│                                    │
│ ▸ Diff                             │
│ ┌────────────────────────────────┐ │
│ │ diff --git a/src/parser.py ... │ │
│ └────────────────────────────────┘ │
│                                    │
├────────────────────────────────────┤
│ [textarea: ask Codex...]           │
│ [Send] [Stop] [Diff] [Files] [Clear]│
└────────────────────────────────────┘
```

Desktop can use the same layout at a wider max width, or optionally show a file panel beside the transcript. Do not spend much code on desktop-specific layout.

### Top bar

The top bar should be sticky at the top and compact.

Show:

```text
Codex Web
repo/root basename
status indicator
logout link/button
```

Status examples:

```text
● idle
● running
● done
● stopped
● error
```

Keep the status text small but visible. Color is optional; do not rely on color alone.

### Transcript

The transcript is the main scrolling region.

Render message cards/blocks:

```text
User message:
  label: You
  text: plain text, preserving line breaks

Assistant message:
  label: Codex
  text: Markdown-rendered, sanitized

Terminal/tool output:
  label: Command output or stderr
  body: collapsible <details><summary>...</summary><pre>...</pre></details>

Diff output:
  label: Diff
  body: collapsible pre block, syntax-highlight as diff if easy

Errors:
  label: Error
  body: plain text, visually distinct
```

Terminal output must not be Markdown-rendered. Escape it and put it in `<pre>`.

Assistant Markdown should be rendered with `marked`, sanitized with `DOMPurify`, and highlighted with `highlight.js`.

### Bottom composer

The composer should be sticky at the bottom.

It contains:

```text
multiline textarea
Send button
Stop button
Diff button
Files button
Clear button
```

Behavior:

- `Send` is disabled while Codex is running.
- `Stop` is enabled only while Codex is running.
- `Diff`, `Files`, and `Clear` remain usable when idle.
- Pressing Enter in the textarea should send only if easy and not hostile to mobile. A safe default is: button sends; textarea allows multiline.
- Do not add voice recognition.

Suggested button row on mobile:

```text
[Send] [Stop] [Diff] [Files] [Clear]
```

Use large tap targets. Mobile fat-finger tolerance beats clever density.

### Files panel / modal

The Files button should open a lightweight panel or modal. Prefer the simplest implementation.

Phone layout:

```text
┌────────────────────────────────────┐
│ Files                         Close │
├────────────────────────────────────┤
│ README.md                          │
│ codez.py                       │
│ src/app.py                         │
│ src/parser.py                      │
│ tests/test_parser.py               │
└────────────────────────────────────┘
```

When a file is selected, show a viewer:

```text
┌────────────────────────────────────┐
│ README.md                  Raw Pretty│
├────────────────────────────────────┤
│ # Project                          │
│ Rendered Markdown or highlighted   │
│ source here.                       │
└────────────────────────────────────┘
```

The file viewer can live in the same modal/panel as the file list. Keep navigation simple:

```text
Files list -> tap file -> file viewer -> Back
```

For v0, it is acceptable to replace the list with the viewer and provide a Back button.

File viewer rules:

- Markdown files: `Raw` and `Pretty`.
- Code files: highlighted source.
- HTML files: highlighted source by default, not live rendering.
- Binary/large/unsafe files: show a clear error.
- No editing.

### Diff panel

The Diff button should request `git diff -- .` from the server and render the result in a collapsible block or modal.

If there is no diff, show:

```text
No changes.
```

If including `git status --short`, show status above diff:

```text
Status
 M src/parser.py
?? tests/test_parser.py

Diff
diff --git ...
```

Do not add a commit button in v0.

### CSS requirements

Use CSS that works well on phones:

```css
html, body {
  height: 100%;
  margin: 0;
}
.app {
  height: 100dvh;
  display: flex;
  flex-direction: column;
}
.transcript {
  flex: 1;
  overflow-y: auto;
}
.composer {
  position: sticky;
  bottom: 0;
}
textarea {
  width: 100%;
  min-height: 4rem;
}
pre {
  overflow-x: auto;
  white-space: pre-wrap;
}
button, textarea {
  font-size: 16px;
}
```

Use `100dvh` rather than only `100vh` for mobile Safari.

Prefer a dark theme by default:

```text
background: near-black
text: near-white
cards: slightly lighter dark
borders: subtle gray
links/buttons: clear but not flashy
```

Do not spend time on elaborate visual polish. Clean, readable, and phone-friendly is the target.

### Scrolling behavior

The UI should auto-scroll while the user is near the bottom. If the user scrolls up, do not violently yank the scroll back down every chunk.

Simple rule:

```javascript
const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
// append content
if (nearBottom) el.scrollTop = el.scrollHeight;
```

### Empty/loading states

Initial transcript can show a short hint:

```text
Ask Codex to inspect, edit, test, or explain this repo.
```

While running, show a small running indicator near the composer or in the top bar. Do not add spinners everywhere. Spinners are where hope goes to die.

---

## Frontend libraries via CDN

Use pinned CDN URLs.

Suggested libraries:

```html
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.0.11/dist/purify.min.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/styles/github-dark.min.css">
<script src="https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/lib/common.min.js"></script>
```

Use:

- `marked` for Markdown
- `DOMPurify` to sanitize rendered Markdown/HTML
- `highlight.js` for syntax highlighting

If CDN loading fails, raw text should still be readable.

---

## Rendering chat and files

Assistant chat messages:

1. Render Markdown with `marked`.
2. Sanitize with `DOMPurify`.
3. Apply syntax highlighting to code blocks.

Terminal output:

- Render as `<pre>`.
- Escape HTML.
- Use monospace.
- Do not Markdown-render terminal output.

File viewer modes by extension:

```text
.md, .markdown         -> markdown
.py, .js, .ts, .css,
.json, .sh, .html,
.htm, .yaml, .yml,
.toml, .c, .h, .cpp    -> code/source
everything else text   -> plain text
binary/too-large       -> error
```

HTML files:

- Default to highlighted source, not live rendering.
- If adding preview, use sandboxed iframe only:

```html
<iframe sandbox srcdoc="..."></iframe>
```

For v0, source-only HTML is acceptable and safer.

---

## Read-only file viewer

Include a simple read-only file viewer.

Client sends:

```json
{"type":"files"}
```

Server returns a recursive file list under `ROOT`:

```json
{"type":"files","files":[{"path":"README.md","size":1234},{"path":"src/app.py","size":4567}]}
```

Client sends:

```json
{"type":"file","path":"src/app.py"}
```

Server returns:

```json
{"type":"file","path":"src/app.py","text":"...","mode":"code"}
```

Path safety:

- Reject absolute paths.
- Reject paths containing `..` after normalization.
- Resolve real path and ensure it starts with `ROOT`.
- Hide `.git`.
- Hide `.codex-web`.
- Skip common large/binary junk directories:

```text
.git
.codex-web
node_modules
__pycache__
.venv
venv
dist
build
target
.cache
```

File size limit:

```text
1 MB max for viewing
```

Binary detection:

- Read a small chunk in binary mode.
- If it contains `\0`, treat as binary and refuse.
- Decode text as UTF-8 with replacement.

Do not implement editing.

---

## Diff button

Include a Diff button.

Client sends:

```json
{"type":"diff"}
```

Server runs:

```bash
git -C "$ROOT" diff -- .
```

Return:

```json
{"type":"diff","text":"diff --git ..."}
```

Do not use Codex for diff. Use `git` directly.

Diff output should render as syntax-highlighted diff if easy, otherwise plain `<pre>`.

Optional but useful:

```bash
git -C "$ROOT" status --short
```

You may include status above the diff.

---

## Logging

Add tiny app-side JSONL logging.

Directory:

```text
<ROOT>/.codex-web/
```

File:

```text
<ROOT>/.codex-web/history.jsonl
```

Create the directory if needed.

Log events such as:

```json
{"ts":1713980000.1,"type":"user","text":"fix parser"}
{"ts":1713980001.2,"type":"codex_event","event":{"type":"turn.started"}}
{"ts":1713980004.7,"type":"assistant","text":"I updated parser.py..."}
{"ts":1713980005.1,"type":"done","exit":0}
{"ts":1713980006.3,"type":"stop"}
{"ts":1713980007.4,"type":"error","text":"..."}
```

This log is not the source of Codex memory. Codex keeps its own local session history. This log is only for browser transcript/debug/audit convenience.

Do not build a database.

---

## Clear button

The Clear button should clear the browser transcript only.

It should not delete Codex history.

Client sends:

```json
{"type":"clear"}
```

Server may clear in-memory scrollback and reply:

```json
{"type":"status","state":"cleared"}
```

Do not reset `FIRST_TURN` unless you intentionally add a New Chat button. For v0, no New Chat button is required.

---

## Thread/session behavior

Use `FIRST_TURN`:

- Before any successful Codex turn: run `codex exec ... -`
- After first successful turn starts/completes: run `codex exec resume --last ... -`

For v0, `resume --last` is acceptable because the app assumes one active chat per repo/server.

Known limitation:

```text
If multiple Codex sessions are run manually in the same repo while this server is active, `resume --last` may resume the wrong session.
```

Do not solve this in v0 unless it is easy. V1 should capture the `thread_id` from `thread.started` and resume an explicit session id if Codex CLI supports that exact id for exec resume in the current installed version.

---

## Error handling

Handle these cleanly:

- `codex` not found in PATH
- root does not contain `.git`
- bad login
- WebSocket auth failure
- JSON parse failure from Codex stdout
- Codex nonzero exit
- user hits Stop
- file not found
- unsafe file path
- binary file
- file too large
- git diff failure

Never crash the whole server because one WebSocket message was malformed.

---

## What not to build in v0

Do not implement:

```text
multi-user accounts
OAuth
database storage
file editing
Monaco editor
terminal emulator
Codex interactive TUI wrapper
Codex app-server
agent DAG view
multi-session picker
git commit button
git branch switching
file upload
voice
MemoriesDB integration
Docker build system
```

Docker is v1 hardening unless trivial.

---

## Docker notes for v1 or optional v0 hardening

The design should be compatible with running inside Docker.

Practical dev run shape:

```bash
docker run --rm -it \
  -v "$PWD:/work" \
  -v "$HOME/.codex:/root/.codex" \
  -p 127.0.0.1:8080:8080 \
  codex-web \
  python /app/codez.py --host=0.0.0.0 --root=/work
```

Notes:

- Mount only the repo you want Codex to work on.
- Do not mount the Docker socket.
- Prefer binding host port to `127.0.0.1`.
- A read-only `~/.codex` mount may break login/session resume, so v0 can mount it read-write.
- A safer v1 should use a project-specific `CODEX_HOME`, non-root user, and tighter volume permissions.

---

## Implementation order

Implement in this order:

1. `docopt` CLI parse.
2. Root normalization and `.git` check.
3. Bottle app and login/logout with signed cookie.
4. Serve the single HTML page.
5. WebSocket auth check.
6. Minimal browser WebSocket connect/send/render loop.
7. Run first Codex turn with `codex exec --json ... -`.
8. Parse JSONL and render assistant messages.
9. Add resume behavior with `codex exec resume --last ... -`.
10. Add Stop button and process-group kill ladder.
11. Add stderr forwarding.
12. Add `.codex-web/history.jsonl`.
13. Add Diff button.
14. Add read-only file list and file viewer.
15. Add Markdown/code rendering with CDN libraries.
16. Polish mobile CSS.

---

## Acceptance tests

Manual tests are fine for v0.

### Startup

From a Git repo:

```bash
python codez.py
```

Expected:

```text
server starts on 127.0.0.1:8080
```

From a non-Git directory:

```text
server exits with a clear error
```

### Login

Visit:

```text
http://127.0.0.1:8080/
```

Expected:

```text
redirects to /login
valid user/password logs in
invalid credentials fail
```

### Chat first turn

Send:

```text
Say PING and do not edit files.
```

Expected:

```text
Codex runs once
assistant message appears in chat
status returns to idle/done
```

### Chat resume

Send:

```text
What did I just ask you to say?
```

Expected:

```text
server uses `codex exec resume --last`
Codex remembers previous turn
```

### Stop

Send a prompt likely to run for a bit, then hit Stop.

Expected:

```text
Codex process group receives interrupt/termination
UI shows stopped
new prompt can be sent after stop
```

### Diff

Click Diff.

Expected:

```text
git diff output appears
no Codex call is made
```

### Files

Click Files.

Expected:

```text
file list appears
.git is hidden
large/binary files are not viewable
README.md renders as Markdown
Python files render with syntax highlighting
HTML files show source, not live unsandboxed HTML
```

### Safety

Try these paths:

```text
../secret.txt
/etc/passwd
.git/config
```

Expected:

```text
all rejected
```

---

## Final design summary

The finished v0 should be:

```text
single-file Python app
mobile-first browser chat
WebSocket I/O
Codex JSONL streaming
turn-based `codex exec`
resume with `codex exec resume --last`
workspace-write sandbox
approval never
signed-cookie login
one active job globally
read-only pretty file viewer
git diff viewer
tiny JSONL app log
no terminal emulator
no app-server
no public-internet exposure without extra protection
```

Build the smallest thing that works cleanly. No framework cosplay.
