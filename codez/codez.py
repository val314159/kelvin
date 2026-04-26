#!/usr/bin/env python3
"""Codex Web: one-file mobile-first Codex CLI wrapper.

Usage:
  codez.py [--host=<host>] [--port=<port>] [--root=<dir>] [--base-path=<path>] [--user=<user>] [--password=<password>] [--secret=<secret>]
  codez.py -h | --help

Options:
  --host=<host>          Bind host [default: 127.0.0.1]
  --port=<port>          Bind port [default: 8080]
  --root=<dir>           Workspace root [default: .]
  --base-path=<path>     Public URL mount path, such as /z [default: /]
  --user=<user>          Login user [default: val]
  --password=<password>  Login password [default: change-me]
  --secret=<secret>      Cookie signing secret [default: change-this-cookie-secret]
  -h --help              Show help.
"""
import sys; sys.dont_write_bytecode = True
import gevent.monkey as _;_.patch_all()
import hashlib
import html
import json
import os
import posixpath
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass

import gevent
import gevent.subprocess as gsubprocess
from gevent.lock import Semaphore
from gevent.pywsgi import WSGIServer
from geventwebsocket.exceptions import WebSocketError
from geventwebsocket.handler import WebSocketHandler

from bottle import Bottle, HTTPResponse, request, response
from docopt import docopt


ROOT = None
BASE_PATH = ""
USER = None
PASSWORD = None
SECRET = None
LOG_DIR = None
LOG_PATH = None

ACTIVE = None
FIRST_TURN = True
CODEX_SESSION_ID = None
SCROLLBACK = []

COOKIE_NAME = "codez_user"
MAX_SCROLLBACK = 300
MAX_FILE_SIZE = 1024 * 1024

ACTIVE_LOCK = Semaphore()
LOG_LOCK = Semaphore()
SCROLLBACK_LOCK = Semaphore()

APP = Bottle()

SKIP_DIRS = {
    ".git",
    ".codex-web",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    "target",
    ".cache",
}

MARKDOWN_EXTS = {".md", ".markdown"}
CODE_EXTS = {
    ".py",
    ".js",
    ".ts",
    ".css",
    ".json",
    ".sh",
    ".html",
    ".htm",
    ".yaml",
    ".yml",
    ".toml",
    ".c",
    ".h",
    ".cpp",
}


LOGIN_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Codex Web Login</title>
  <style>
    :root { color-scheme: dark; }
    * { box-sizing: border-box; }
    html, body { height: 100%; margin: 0; }
    body {
      min-height: 100dvh;
      display: grid;
      place-items: center;
      background: #0b0d10;
      color: #eef2f6;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      padding: 1rem;
    }
    main {
      width: min(100%, 24rem);
      border: 1px solid #2b313a;
      background: #131720;
      border-radius: 8px;
      padding: 1rem;
      box-shadow: 0 1rem 3rem rgba(0, 0, 0, .35);
    }
    h1 { font-size: 1.25rem; margin: 0 0 .25rem; }
    p { color: #aeb8c5; line-height: 1.4; margin: .25rem 0 1rem; }
    label { display: block; margin: .8rem 0 .35rem; color: #dbe3ec; }
    input, button {
      width: 100%;
      min-height: 2.75rem;
      border-radius: 8px;
      border: 1px solid #303846;
      font-size: 16px;
    }
    input {
      background: #0c1118;
      color: #f2f6fb;
      padding: .65rem .75rem;
    }
    button {
      margin-top: 1rem;
      background: #3d7eff;
      color: white;
      font-weight: 650;
      cursor: pointer;
    }
    .error {
      border: 1px solid #7c2d32;
      background: #2a1014;
      color: #ffb8bd;
      border-radius: 8px;
      padding: .65rem .75rem;
      margin: .75rem 0 0;
    }
    .note { font-size: .82rem; color: #8793a2; margin-top: 1rem; }
  </style>
</head>
<body>
  <main>
    <h1>Codex Web</h1>
    <p>Sign in to use this local Codex control panel.</p>
    __ERROR__
    <form method="post" action="__LOGIN_ACTION__">
      <label for="user">User</label>
      <input id="user" name="user" autocomplete="username" value="__USER__">
      <label for="password">Password</label>
      <input id="password" name="password" type="password" autocomplete="current-password">
      <button type="submit">Log in</button>
    </form>
    <p class="note">Local/private use only. Put HTTPS and another authentication layer in front before remote exposure.</p>
  </main>
</body>
</html>"""


APP_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Codex Web</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/styles/github-dark.min.css">
  <style>
    :root {
      color-scheme: dark;
      --bg: #090b0f;
      --panel: #111620;
      --card: #151b26;
      --card-2: #0f141d;
      --line: #28313f;
      --text: #edf2f7;
      --muted: #9aa7b7;
      --accent: #4f8cff;
      --accent-2: #65d6ad;
      --danger: #ff6b7a;
      --warning: #f5c463;
    }
    * { box-sizing: border-box; }
    html, body {
      height: 100%;
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    body { overflow: hidden; }
    button, textarea { font-family: inherit; font-size: 16px; }
    button, a { -webkit-tap-highlight-color: transparent; }
    .app {
      height: 100dvh;
      display: flex;
      flex-direction: column;
      max-width: 58rem;
      margin: 0 auto;
      border-inline: 1px solid rgba(255, 255, 255, .04);
      background: var(--bg);
    }
    .topbar {
      position: sticky;
      top: 0;
      z-index: 5;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: .75rem;
      padding: .7rem .85rem;
      border-bottom: 1px solid var(--line);
      background: rgba(13, 17, 24, .96);
      backdrop-filter: blur(10px);
    }
    .brand {
      min-width: 0;
      display: flex;
      flex-direction: column;
      gap: .1rem;
    }
    .brand strong { font-size: 1rem; line-height: 1.1; }
    .repo {
      color: var(--muted);
      font-size: .78rem;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      max-width: 56vw;
    }
    .top-actions {
      display: flex;
      align-items: center;
      gap: .6rem;
      flex: 0 0 auto;
    }
    .status {
      display: inline-flex;
      align-items: center;
      gap: .35rem;
      color: var(--muted);
      font-size: .82rem;
      white-space: nowrap;
    }
    .status-dot {
      width: .55rem;
      height: .55rem;
      border-radius: 999px;
      background: #7a8593;
      box-shadow: 0 0 0 1px rgba(255, 255, 255, .15);
    }
    .status-dot.running { background: var(--warning); }
    .status-dot.done, .status-dot.idle { background: var(--accent-2); }
    .status-dot.error { background: var(--danger); }
    .topbar a {
      color: #c8d7ff;
      text-decoration: none;
      font-size: .84rem;
    }
    .transcript {
      flex: 1;
      overflow-y: auto;
      padding: .85rem;
    }
    .empty {
      color: var(--muted);
      border: 1px dashed #303846;
      background: #0d121a;
      border-radius: 8px;
      padding: 1rem;
      line-height: 1.45;
    }
    .block {
      margin: 0 0 .85rem;
      border: 1px solid var(--line);
      background: var(--card);
      border-radius: 8px;
      padding: .78rem;
    }
    .block.user { background: #121923; }
    .block.assistant { background: #151b26; }
    .block.error {
      border-color: #69313a;
      background: #231015;
    }
    .label {
      color: var(--muted);
      font-size: .78rem;
      font-weight: 700;
      letter-spacing: 0;
      margin-bottom: .45rem;
    }
    .plain {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      line-height: 1.45;
    }
    .markdown {
      line-height: 1.48;
      overflow-wrap: anywhere;
    }
    .markdown :first-child { margin-top: 0; }
    .markdown :last-child { margin-bottom: 0; }
    .markdown pre { background: #0b1018; border: 1px solid #2a3340; border-radius: 8px; padding: .75rem; }
    details.tool {
      border: 1px solid var(--line);
      background: var(--card-2);
      border-radius: 8px;
      overflow: hidden;
    }
    details.tool + details.tool { margin-top: .6rem; }
    details.tool summary {
      cursor: pointer;
      min-height: 2.5rem;
      display: flex;
      align-items: center;
      padding: .55rem .7rem;
      color: #dce7f3;
      font-weight: 650;
    }
    pre {
      overflow-x: auto;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      margin: 0;
      padding: .75rem;
      background: #090e15;
      color: #edf4fb;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      font-size: .86rem;
      line-height: 1.45;
    }
    .composer {
      position: sticky;
      bottom: 0;
      z-index: 4;
      border-top: 1px solid var(--line);
      background: rgba(13, 17, 24, .98);
      padding: .75rem;
      padding-bottom: max(.75rem, env(safe-area-inset-bottom));
    }
    textarea {
      width: 100%;
      min-height: 4rem;
      resize: vertical;
      border-radius: 8px;
      border: 1px solid #303846;
      background: #0b1119;
      color: var(--text);
      padding: .75rem;
      outline: none;
      line-height: 1.4;
    }
    textarea:focus { border-color: #527fce; box-shadow: 0 0 0 2px rgba(79, 140, 255, .18); }
    .button-row {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: .45rem;
      margin-top: .55rem;
    }
    button {
      min-height: 2.75rem;
      border-radius: 8px;
      border: 1px solid #344052;
      background: #182231;
      color: #eef4fb;
      cursor: pointer;
      font-weight: 650;
    }
    button.primary { background: var(--accent); border-color: #6fa1ff; color: white; }
    button.danger { color: #ffd3d7; border-color: #70404a; }
    button:disabled {
      cursor: not-allowed;
      opacity: .48;
    }
    .run-note {
      min-height: 1.2rem;
      color: var(--muted);
      font-size: .78rem;
      margin-top: .4rem;
    }
    .modal {
      position: fixed;
      inset: 0;
      z-index: 20;
      background: rgba(0, 0, 0, .65);
      display: grid;
      place-items: stretch;
      padding: .75rem;
    }
    .modal[hidden] { display: none; }
    .modal-panel {
      width: min(100%, 60rem);
      height: min(100%, 48rem);
      margin: auto;
      display: flex;
      flex-direction: column;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #0e131b;
      box-shadow: 0 1rem 4rem rgba(0, 0, 0, .6);
      overflow: hidden;
    }
    .modal-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: .6rem;
      padding: .7rem;
      border-bottom: 1px solid var(--line);
      background: #121923;
    }
    .modal-title {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-weight: 750;
    }
    .modal-actions {
      display: flex;
      align-items: center;
      gap: .45rem;
      flex: 0 0 auto;
    }
    .modal-actions button {
      min-height: 2.35rem;
      padding: 0 .7rem;
    }
    .modal-body {
      flex: 1;
      overflow: auto;
      padding: .7rem;
    }
    .file-list {
      display: grid;
      gap: .35rem;
    }
    .file-row {
      display: flex;
      justify-content: space-between;
      gap: .75rem;
      width: 100%;
      text-align: left;
      background: #111822;
      color: var(--text);
      border: 1px solid #273241;
      border-radius: 8px;
      padding: .65rem .7rem;
      min-height: 2.7rem;
    }
    .file-path {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .file-size {
      color: var(--muted);
      font-size: .8rem;
      flex: 0 0 auto;
    }
    .viewer-source, .viewer-pretty {
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      background: #090e15;
    }
    .viewer-pretty {
      padding: .8rem;
      line-height: 1.5;
    }
    .editor-host {
      min-height: 100%;
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      background: #090e15;
    }
    .editor-host .cm-editor {
      min-height: min(34rem, calc(100dvh - 9rem));
      background: #090e15;
      color: var(--text);
      font-size: 16px;
    }
    .editor-host .cm-content {
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
    }
    .editor-host textarea.editor-fallback {
      min-height: min(34rem, calc(100dvh - 9rem));
      border: 0;
      border-radius: 0;
      resize: none;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
    }
    @media (max-width: 430px) {
      .button-row { grid-template-columns: repeat(5, minmax(3.25rem, 1fr)); gap: .35rem; }
      button { min-height: 2.65rem; font-size: .9rem; }
      .topbar { padding-inline: .7rem; }
      .transcript { padding: .7rem; }
      .composer { padding: .65rem; }
      .modal { padding: 0; }
      .modal-panel { height: 100%; width: 100%; border-radius: 0; border-inline: 0; }
    }
  </style>
</head>
<body>
  <div class="app">
    <header class="topbar">
      <div class="brand">
        <strong>Codex Web</strong>
        <span class="repo" id="repoName"></span>
      </div>
      <div class="top-actions">
        <span class="status" aria-live="polite">
          <span class="status-dot" id="statusDot"></span>
          <span id="statusText">connecting</span>
        </span>
        <a href="__LOGOUT_URL__">logout</a>
      </div>
    </header>

    <main class="transcript" id="transcript">
      <section class="empty" id="emptyState">Ask Codex to inspect, edit, test, or explain this repo.</section>
    </main>

    <form class="composer" id="composer">
      <textarea id="prompt" placeholder="Ask Codex..." autocomplete="off"></textarea>
      <div class="button-row">
        <button class="primary" id="sendBtn" type="submit">Send</button>
        <button class="danger" id="stopBtn" type="button" disabled>Stop</button>
        <button id="diffBtn" type="button">Diff</button>
        <button id="filesBtn" type="button">Files</button>
        <button id="clearBtn" type="button">Clear</button>
      </div>
      <div class="run-note" id="runNote"></div>
    </form>
  </div>

  <div class="modal" id="fileModal" hidden>
    <div class="modal-panel">
      <div class="modal-head">
        <div class="modal-title" id="modalTitle">Files</div>
        <div class="modal-actions" id="modalActions">
          <button id="closeFilesBtn" type="button">Close</button>
        </div>
      </div>
      <div class="modal-body" id="modalBody"></div>
    </div>
  </div>

  <script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/dompurify@3.0.11/dist/purify.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/lib/common.min.js"></script>
  <script>
    const repoName = __REPO_JSON__;
    const rootPath = __ROOT_JSON__;
    const basePath = __BASE_PATH_JSON__;
    const transcript = document.getElementById("transcript");
    const emptyState = document.getElementById("emptyState");
    const promptBox = document.getElementById("prompt");
    const composer = document.getElementById("composer");
    const sendBtn = document.getElementById("sendBtn");
    const stopBtn = document.getElementById("stopBtn");
    const diffBtn = document.getElementById("diffBtn");
    const filesBtn = document.getElementById("filesBtn");
    const clearBtn = document.getElementById("clearBtn");
    const runNote = document.getElementById("runNote");
    const statusText = document.getElementById("statusText");
    const statusDot = document.getElementById("statusDot");
    const fileModal = document.getElementById("fileModal");
    const modalTitle = document.getElementById("modalTitle");
    const modalActions = document.getElementById("modalActions");
    const modalBody = document.getElementById("modalBody");
    const closeFilesBtn = document.getElementById("closeFilesBtn");

    let ws = null;
    let running = false;
    let connected = false;
    let files = [];
    let currentFile = null;
    let markdownPretty = true;
    let fileEditMode = false;
    let editorView = null;
    let editorFallback = null;
    let codeMirrorPromise = null;
    let autoScroll = true;
    let scrollTimer = null;

    document.getElementById("repoName").textContent = "repo: " + repoName;
    document.getElementById("repoName").title = rootPath;

    function socketUrl() {
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      return scheme + "://" + location.host + basePath + "/ws";
    }

    function connect() {
      ws = new WebSocket(socketUrl());
      ws.addEventListener("open", () => {
        connected = true;
        setStatus("idle");
        updateButtons();
      });
      ws.addEventListener("message", (event) => {
        let msg;
        try {
          msg = JSON.parse(event.data);
        } catch (err) {
          appendError("Bad server message: " + event.data);
          return;
        }
        handleMessage(msg);
      });
      ws.addEventListener("close", () => {
        connected = false;
        running = false;
        setStatus("error", "disconnected");
        updateButtons();
      });
      ws.addEventListener("error", () => {
        connected = false;
        setStatus("error", "socket error");
        updateButtons();
      });
    }

    function sendMessage(msg) {
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        appendError("WebSocket is not connected.");
        return false;
      }
      ws.send(JSON.stringify(msg));
      return true;
    }

    function setStatus(state, text) {
      const label = text || state;
      statusText.textContent = label;
      statusDot.className = "status-dot " + state;
      running = state === "running";
      runNote.textContent = running ? "Codex is running." : "";
      updateButtons();
    }

    function updateButtons() {
      sendBtn.disabled = running || !connected;
      stopBtn.disabled = !running || !connected;
      diffBtn.disabled = !connected;
      filesBtn.disabled = !connected;
      clearBtn.disabled = false;
    }

    function nearBottom() {
      return transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 80;
    }

    function scrollToBottom() {
      autoScroll = true;
      if (scrollTimer !== null) return;
      scrollTimer = window.setTimeout(stepReaderScroll, 250);
    }

    function stepReaderScroll() {
      scrollTimer = null;
      if (!autoScroll) return;

      const maxTop = transcript.scrollHeight - transcript.clientHeight;
      const remaining = maxTop - transcript.scrollTop;
      if (remaining <= 1) {
        transcript.scrollTop = maxTop;
        return;
      }

      const lineStep = 44;
      transcript.scrollTo({
        top: Math.min(maxTop, transcript.scrollTop + lineStep),
        behavior: "smooth"
      });
      scrollTimer = window.setTimeout(stepReaderScroll, 600);
    }

    function afterAppend(wasNearBottom) {
      if (emptyState) {
        emptyState.hidden = transcript.querySelectorAll(".block").length > 0;
      }
      if (wasNearBottom || autoScroll) scrollToBottom();
    }

    transcript.addEventListener("scroll", () => {
      if (nearBottom()) autoScroll = true;
    });
    ["wheel", "touchmove"].forEach((eventName) => {
      transcript.addEventListener(eventName, () => {
        if (!nearBottom()) autoScroll = false;
      }, { passive: true });
    });

    function makeBlock(kind, label) {
      const wasNearBottom = nearBottom();
      const block = document.createElement("section");
      block.className = "block " + kind;
      const labelEl = document.createElement("div");
      labelEl.className = "label";
      labelEl.textContent = label;
      block.appendChild(labelEl);
      transcript.appendChild(block);
      return { block, wasNearBottom };
    }

    function appendPlain(kind, label, text) {
      const created = makeBlock(kind, label);
      const body = document.createElement("div");
      body.className = "plain";
      body.textContent = text || "";
      created.block.appendChild(body);
      afterAppend(created.wasNearBottom);
    }

    function renderMarkdownInto(el, text) {
      if (window.marked && window.DOMPurify) {
        el.innerHTML = DOMPurify.sanitize(marked.parse(text || ""));
        highlightInside(el);
      } else {
        el.textContent = text || "";
      }
    }

    function highlightInside(el) {
      if (!window.hljs) return;
      el.querySelectorAll("pre code").forEach((code) => hljs.highlightElement(code));
    }

    function appendAssistant(text) {
      const created = makeBlock("assistant", "Codex");
      const body = document.createElement("div");
      body.className = "markdown";
      renderMarkdownInto(body, text);
      created.block.appendChild(body);
      afterAppend(created.wasNearBottom);
    }

    function appendTool(label, text, language, open) {
      const wasNearBottom = nearBottom();
      if (emptyState) emptyState.hidden = true;
      const details = document.createElement("details");
      details.className = "tool block";
      if (open) details.open = true;
      const summary = document.createElement("summary");
      summary.textContent = label;
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      if (language) code.className = "language-" + language;
      code.textContent = text || "";
      pre.appendChild(code);
      details.appendChild(summary);
      details.appendChild(pre);
      transcript.appendChild(details);
      if (window.hljs && language) hljs.highlightElement(code);
      afterAppend(wasNearBottom);
    }

    function appendError(text) {
      appendPlain("error", "Error", text || "Something went wrong.");
    }

    function handleMessage(msg) {
      if (!msg || typeof msg !== "object") return;
      if (msg.type === "status") {
        if (msg.state === "cleared") {
          setStatus("idle");
        } else {
          setStatus(msg.state || "idle");
        }
      } else if (msg.type === "chat") {
        if (msg.role === "assistant") appendAssistant(msg.text || "");
        else appendPlain("user", "You", msg.text || "");
      } else if (msg.type === "terminal") {
        const label = msg.stream === "stderr" ? "stderr" : "Command output";
        appendTool(label, msg.text || "", "", false);
      } else if (msg.type === "diff") {
        appendTool("Diff", msg.text || "No changes.", "diff", true);
      } else if (msg.type === "files") {
        files = msg.files || [];
        renderFileList();
      } else if (msg.type === "file") {
        currentFile = msg;
        markdownPretty = msg.mode === "markdown";
        fileEditMode = false;
        renderFileViewer();
      } else if (msg.type === "error") {
        appendError(msg.text);
        setStatus("error");
      } else if (msg.type === "codex_event") {
        // Keep these available to protocol consumers without cluttering the v0 UI.
      }
    }

    composer.addEventListener("submit", (event) => {
      event.preventDefault();
      const text = promptBox.value.trim();
      if (!text || running) return;
      if (sendMessage({ type: "prompt", text })) {
        promptBox.value = "";
      }
    });

    stopBtn.addEventListener("click", () => sendMessage({ type: "stop" }));
    diffBtn.addEventListener("click", () => sendMessage({ type: "diff" }));
    filesBtn.addEventListener("click", () => {
      openFiles();
      sendMessage({ type: "files" });
    });
    clearBtn.addEventListener("click", () => {
      transcript.querySelectorAll(".block, details.tool").forEach((el) => el.remove());
      if (emptyState) emptyState.hidden = false;
      sendMessage({ type: "clear" });
    });
    closeFilesBtn.addEventListener("click", () => closeFiles());

    function openFiles() {
      fileModal.hidden = false;
      modalTitle.textContent = "Files";
      modalBody.innerHTML = '<div class="plain">Loading...</div>';
      renderCloseOnlyActions();
    }

    function closeFiles() {
      destroyEditor();
      fileModal.hidden = true;
      currentFile = null;
      fileEditMode = false;
    }

    function renderCloseOnlyActions() {
      modalActions.innerHTML = "";
      const close = document.createElement("button");
      close.type = "button";
      close.textContent = "Close";
      close.addEventListener("click", closeFiles);
      modalActions.appendChild(close);
    }

    function renderFileList() {
      destroyEditor();
      currentFile = null;
      fileEditMode = false;
      modalTitle.textContent = "Files";
      renderCloseOnlyActions();
      modalBody.innerHTML = "";
      if (!files.length) {
        const empty = document.createElement("div");
        empty.className = "plain";
        empty.textContent = "No files.";
        modalBody.appendChild(empty);
        return;
      }
      const list = document.createElement("div");
      list.className = "file-list";
      files.forEach((file) => {
        const row = document.createElement("button");
        row.type = "button";
        row.className = "file-row";
        const path = document.createElement("span");
        path.className = "file-path";
        path.textContent = file.path;
        const size = document.createElement("span");
        size.className = "file-size";
        size.textContent = formatSize(file.size || 0);
        row.append(path, size);
        row.addEventListener("click", () => sendMessage({ type: "file", path: file.path }));
        list.appendChild(row);
      });
      modalBody.appendChild(list);
    }

    function renderFileViewer() {
      if (!currentFile) return;
      destroyEditor();
      fileEditMode = false;
      modalTitle.textContent = currentFile.path || "File";
      modalActions.innerHTML = "";
      const back = document.createElement("button");
      back.type = "button";
      back.textContent = "Back";
      back.addEventListener("click", renderFileList);
      modalActions.appendChild(back);
      if (currentFile.mode === "markdown") {
        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.textContent = markdownPretty ? "Raw" : "Pretty";
        toggle.addEventListener("click", () => {
          markdownPretty = !markdownPretty;
          renderFileViewer();
        });
        modalActions.appendChild(toggle);
      }
      if (currentFile.editable) {
        const edit = document.createElement("button");
        edit.type = "button";
        edit.textContent = "Edit";
        edit.addEventListener("click", renderFileEditor);
        modalActions.appendChild(edit);
      }
      const close = document.createElement("button");
      close.type = "button";
      close.textContent = "Close";
      close.addEventListener("click", closeFiles);
      modalActions.appendChild(close);
      modalBody.innerHTML = "";
      if (currentFile.mode === "markdown" && markdownPretty) {
        const body = document.createElement("div");
        body.className = "viewer-pretty markdown";
        renderMarkdownInto(body, currentFile.text || "");
        modalBody.appendChild(body);
      } else {
        const pre = document.createElement("pre");
        pre.className = "viewer-source";
        const code = document.createElement("code");
        const language = languageForPath(currentFile.path || "", currentFile.mode);
        if (language) code.className = "language-" + language;
        code.textContent = currentFile.text || "";
        pre.appendChild(code);
        modalBody.appendChild(pre);
        if (window.hljs && language) hljs.highlightElement(code);
      }
    }

    function loadCodeMirror() {
      if (!codeMirrorPromise) {
        codeMirrorPromise = import("https://esm.sh/codemirror@6.0.1");
      }
      return codeMirrorPromise;
    }

    function destroyEditor() {
      if (editorView) {
        editorView.destroy();
        editorView = null;
      }
      editorFallback = null;
    }

    function renderFileEditor() {
      if (!currentFile || !currentFile.editable) return;
      destroyEditor();
      fileEditMode = true;
      markdownPretty = false;
      modalTitle.textContent = currentFile.path || "File";
      modalActions.innerHTML = "";

      const save = document.createElement("button");
      save.type = "button";
      save.textContent = "Save";
      save.addEventListener("click", saveCurrentFile);
      modalActions.appendChild(save);

      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.textContent = "Cancel";
      cancel.addEventListener("click", renderFileViewer);
      modalActions.appendChild(cancel);

      const close = document.createElement("button");
      close.type = "button";
      close.textContent = "Close";
      close.addEventListener("click", closeFiles);
      modalActions.appendChild(close);

      modalBody.innerHTML = "";
      const host = document.createElement("div");
      host.className = "editor-host";
      modalBody.appendChild(host);

      loadCodeMirror()
        .then((cm) => {
          if (!fileEditMode || !currentFile) return;
          editorView = new cm.EditorView({
            doc: currentFile.text || "",
            extensions: [
              cm.basicSetup,
              cm.EditorView.lineWrapping,
              cm.EditorView.theme({
                "&": { backgroundColor: "#090e15", color: "#edf2f7" },
                ".cm-gutters": { backgroundColor: "#0f141d", color: "#9aa7b7", borderRightColor: "#28313f" },
                ".cm-activeLine": { backgroundColor: "#131b27" },
                ".cm-activeLineGutter": { backgroundColor: "#182231" },
                ".cm-cursor": { borderLeftColor: "#edf2f7" },
                ".cm-selectionBackground, &.cm-focused .cm-selectionBackground": { backgroundColor: "#254d8f" }
              })
            ],
            parent: host
          });
          editorView.focus();
        })
        .catch(() => {
          if (!fileEditMode || !currentFile) return;
          editorFallback = document.createElement("textarea");
          editorFallback.className = "editor-fallback";
          editorFallback.value = currentFile.text || "";
          host.appendChild(editorFallback);
          editorFallback.focus();
        });
    }

    function editorText() {
      if (editorView) return editorView.state.doc.toString();
      if (editorFallback) return editorFallback.value;
      return currentFile ? currentFile.text || "" : "";
    }

    function saveCurrentFile() {
      if (!currentFile) return;
      sendMessage({
        type: "write_file",
        path: currentFile.path,
        text: editorText(),
        hash: currentFile.hash
      });
    }

    function languageForPath(path, mode) {
      if (mode === "markdown") return "markdown";
      const ext = path.split(".").pop().toLowerCase();
      const map = {
        py: "python", js: "javascript", ts: "typescript", css: "css",
        json: "json", sh: "bash", html: "html", htm: "html",
        yaml: "yaml", yml: "yaml", toml: "ini", c: "c", h: "c", cpp: "cpp"
      };
      return map[ext] || "";
    }

    function formatSize(size) {
      if (size < 1024) return size + " B";
      if (size < 1024 * 1024) return (size / 1024).toFixed(1) + " KB";
      return (size / (1024 * 1024)).toFixed(1) + " MB";
    }

    connect();
  </script>
</body>
</html>"""


def normalize_base_path(path):
    path = (path or "/").strip()
    if not path or path == "/":
        return ""
    if "?" in path or "#" in path:
        raise SystemExit("error: --base-path must be a path, not a URL")
    if not path.startswith("/"):
        path = "/" + path
    return path.rstrip("/")


def public_path(path):
    if not path.startswith("/"):
        path = "/" + path
    if not BASE_PATH:
        return path
    if path == "/":
        return BASE_PATH + "/"
    return BASE_PATH + path


def redirect_to(path):
    res = response.copy(cls=HTTPResponse)
    res.status = 303
    res.body = ""
    res.set_header("Location", public_path(path))
    return res


def login_page(error=None):
    error_html = ""
    if error:
        error_html = '<div class="error">{}</div>'.format(html.escape(error))
    return (
        LOGIN_HTML.replace("__ERROR__", error_html)
        .replace("__USER__", html.escape(USER or ""))
        .replace("__LOGIN_ACTION__", html.escape(public_path("/login"), quote=True))
    )


def app_page():
    repo = os.path.basename(ROOT.rstrip(os.sep)) or ROOT
    return (
        APP_HTML.replace("__REPO_JSON__", json.dumps(repo))
        .replace("__ROOT_JSON__", json.dumps(ROOT))
        .replace("__BASE_PATH_JSON__", json.dumps(BASE_PATH))
        .replace("__LOGOUT_URL__", html.escape(public_path("/logout"), quote=True))
    )


def current_user():
    try:
        return request.get_cookie(COOKIE_NAME, secret=SECRET)
    except Exception:
        return None


def authenticated():
    return current_user() == USER


def safe_send(ws, payload):
    try:
        ws.send(json.dumps(payload, default=str))
        return True
    except (WebSocketError, OSError, ValueError):
        return False


def remember(payload):
    if payload.get("type") not in {"chat", "terminal", "diff", "error"}:
        return
    with SCROLLBACK_LOCK:
        SCROLLBACK.append(payload)
        if len(SCROLLBACK) > MAX_SCROLLBACK:
            del SCROLLBACK[: len(SCROLLBACK) - MAX_SCROLLBACK]


def emit(ws, payload, keep=True):
    if keep:
        remember(payload)
    safe_send(ws, payload)


def log_event(event_type, **data):
    if not LOG_PATH:
        return
    record = {"ts": time.time(), "type": event_type}
    record.update(data)
    try:
        line = json.dumps(record, default=str, ensure_ascii=False) + "\n"
        with LOG_LOCK:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        pass


def is_benign_codex_stderr(line):
    marker = " ERROR codex_core::session: failed to record rollout items: thread "
    return marker in line and line.endswith(" not found")


def command_for_turn(first_turn, session_id=None):
    base = [
        "codex",
        "--ask-for-approval",
        "never",
        "--sandbox",
        "workspace-write",
        "--cd",
        ROOT,
        "exec",
    ]
    if first_turn:
        base.extend(["--json", "-"])
    else:
        base.append("resume")
        base.append("--json")
        if session_id:
            base.append(session_id)
        else:
            base.append("--last")
        base.append("-")
    return base


def shellish_command(cmd):
    return " ".join(shlex.quote(part) for part in cmd)


def extract_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks = []
        for part in value:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                chunks.append(str(part.get("text") or part.get("content") or ""))
        return "".join(chunks)
    return str(value)


def item_text(item):
    for key in ("text", "message", "content", "output"):
        text = extract_text(item.get(key))
        if text:
            return text
    return ""


def error_text(event):
    for key in ("message", "text", "error"):
        value = event.get(key)
        if not value:
            continue
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return value.get("message") or json.dumps(value, default=str)
        return str(value)
    return json.dumps(event, default=str)


def format_command_execution(item):
    parts = []
    command = item.get("command") or item.get("cmd") or item.get("command_line")
    if isinstance(command, list):
        parts.append("$ " + shellish_command([str(part) for part in command]))
    elif command:
        parts.append("$ " + str(command))

    for key, label in (
        ("stdout", None),
        ("stderr", "stderr"),
        ("output", None),
        ("aggregated_output", None),
    ):
        value = item.get(key)
        if not value:
            continue
        text = extract_text(value).rstrip()
        if not text:
            continue
        if label:
            parts.append(label + ":\n" + text)
        else:
            parts.append(text)

    exit_code = item.get("exit_code")
    if exit_code is None:
        exit_code = item.get("exit")
    if exit_code is not None:
        parts.append("[exit {}]".format(exit_code))

    if not parts:
        parts.append(json.dumps(item, indent=2, default=str))
    return "\n".join(parts)


@dataclass
class CodexJob:
    prompt: str
    ws: object
    first_turn: bool
    proc: subprocess.Popen = None
    stopped: bool = False
    stopped_announced: bool = False
    turn_completed: bool = False
    failed: bool = False

    def run(self):
        global ACTIVE, FIRST_TURN

        try:
            if self.stopped:
                self.announce_stopped()
                return

            cmd = command_for_turn(self.first_turn, CODEX_SESSION_ID)
            emit(self.ws, {"type": "status", "state": "running"}, keep=False)
            emit(self.ws, {"type": "terminal", "text": "$ " + shellish_command(cmd)}, keep=True)

            if shutil.which("codex") is None:
                self.fail("codex not found in PATH")
                return

            try:
                self.proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    cwd=ROOT,
                    start_new_session=True,
                )
            except FileNotFoundError:
                self.fail("codex not found in PATH")
                return
            except OSError as exc:
                self.fail("failed to start codex: {}".format(exc))
                return

            stdout_greenlet = gevent.spawn(self.read_stdout)
            stderr_greenlet = gevent.spawn(self.read_stderr)

            try:
                if self.proc.stdin:
                    self.proc.stdin.write(self.prompt)
                    if not self.prompt.endswith("\n"):
                        self.proc.stdin.write("\n")
                    self.proc.stdin.flush()
                    self.proc.stdin.close()
            except (BrokenPipeError, OSError) as exc:
                emit(self.ws, {"type": "terminal", "stream": "stderr", "text": str(exc)}, keep=True)

            exit_code = self.proc.wait()
            stdout_greenlet.join(timeout=1)
            stderr_greenlet.join(timeout=1)

            if self.stopped:
                self.announce_stopped()
            elif self.failed:
                emit(self.ws, {"type": "status", "state": "error", "exit": exit_code}, keep=False)
            elif exit_code == 0:
                if self.first_turn:
                    FIRST_TURN = False
                emit(self.ws, {"type": "status", "state": "done", "exit": exit_code}, keep=False)
                log_event("done", exit=exit_code)
            else:
                text = "Codex exited with status {}".format(exit_code)
                emit(self.ws, {"type": "error", "text": text}, keep=True)
                emit(self.ws, {"type": "status", "state": "error", "exit": exit_code}, keep=False)
                log_event("error", text=text, exit=exit_code)
        finally:
            with ACTIVE_LOCK:
                if ACTIVE is self:
                    ACTIVE = None

    def fail(self, text):
        global ACTIVE
        emit(self.ws, {"type": "error", "text": text}, keep=True)
        emit(self.ws, {"type": "status", "state": "error"}, keep=False)
        log_event("error", text=text)
        with ACTIVE_LOCK:
            if ACTIVE is self:
                ACTIVE = None

    def read_stdout(self):
        if not self.proc or not self.proc.stdout:
            return
        for raw_line in self.proc.stdout:
            line = raw_line.rstrip("\n")
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                emit(self.ws, {"type": "terminal", "stream": "stdout", "text": line}, keep=True)
                continue
            self.route_codex_event(event)

    def read_stderr(self):
        if not self.proc or not self.proc.stderr:
            return
        for raw_line in self.proc.stderr:
            line = raw_line.rstrip("\n")
            if not line:
                continue
            if is_benign_codex_stderr(line):
                log_event("terminal_suppressed", stream="stderr", text=line)
                continue
            emit(self.ws, {"type": "terminal", "stream": "stderr", "text": line}, keep=True)
            log_event("terminal", stream="stderr", text=line)

    def route_codex_event(self, event):
        global FIRST_TURN, CODEX_SESSION_ID

        log_event("codex_event", event=event)
        event_type = event.get("type")
        item = event.get("item") or {}
        item_type = item.get("type")

        if event_type == "thread.started":
            thread_id = event.get("thread_id")
            if thread_id:
                CODEX_SESSION_ID = thread_id
                log_event("session", session_id=thread_id)
            emit(self.ws, {"type": "codex_event", "event": event}, keep=False)
            return

        if event_type == "turn.started":
            emit(self.ws, {"type": "status", "state": "running"}, keep=False)
            return

        if event_type == "turn.completed":
            self.turn_completed = True
            if self.first_turn:
                FIRST_TURN = False
            emit(self.ws, {"type": "status", "state": "done"}, keep=False)
            log_event("done")
            return

        if event_type in {"turn.failed", "error"}:
            self.failed = True
            text = error_text(event)
            emit(self.ws, {"type": "error", "text": text}, keep=True)
            emit(self.ws, {"type": "status", "state": "error"}, keep=False)
            log_event("error", text=text)
            return

        if event_type == "item.started" and item_type == "command_execution":
            command = item.get("command") or item.get("cmd") or item.get("command_line")
            if command:
                if isinstance(command, list):
                    text = "$ " + shellish_command([str(part) for part in command])
                else:
                    text = "$ " + str(command)
                emit(self.ws, {"type": "terminal", "text": text}, keep=True)
            return

        if event_type == "item.completed":
            if item_type == "agent_message":
                text = item_text(item)
                if text:
                    emit(self.ws, {"type": "chat", "role": "assistant", "text": text}, keep=True)
                    log_event("assistant", text=text)
                return
            if item_type == "command_execution":
                emit(
                    self.ws,
                    {"type": "terminal", "text": format_command_execution(item)},
                    keep=True,
                )
                return
            if item_type == "file_change":
                emit(
                    self.ws,
                    {"type": "terminal", "text": json.dumps(item, indent=2, default=str)},
                    keep=True,
                )
                return
            if item_type in {"reasoning", "plan_update", "web_search", "mcp_tool_call"}:
                emit(self.ws, {"type": "codex_event", "event": event}, keep=False)
                return

        emit(self.ws, {"type": "codex_event", "event": event}, keep=False)

    def stop(self):
        if not self.proc or self.proc.poll() is not None:
            self.stopped = True
            self.announce_stopped()
            return

        self.stopped = True
        log_event("stop")

        for sig, delay in (
            (signal.SIGINT, 1.5),
            (signal.SIGTERM, 1.5),
            (signal.SIGKILL, 0.5),
        ):
            if self.proc.poll() is not None:
                break
            try:
                os.killpg(self.proc.pid, sig)
            except ProcessLookupError:
                break
            except OSError as exc:
                emit(self.ws, {"type": "error", "text": "failed to stop codex: {}".format(exc)}, keep=True)
                break
            deadline = time.time() + delay
            while time.time() < deadline:
                if self.proc.poll() is not None:
                    break
                gevent.sleep(0.05)

        self.announce_stopped()

    def announce_stopped(self):
        if self.stopped_announced:
            return
        self.stopped_announced = True
        emit(self.ws, {"type": "status", "state": "stopped"}, keep=False)


def start_codex_turn(ws, text):
    global ACTIVE

    prompt = (text or "").strip()
    if not prompt:
        emit(ws, {"type": "error", "text": "Prompt is empty"}, keep=True)
        return

    with ACTIVE_LOCK:
        if ACTIVE is not None and (ACTIVE.proc is None or ACTIVE.proc.poll() is None):
            emit(ws, {"type": "error", "text": "Codex is already running"}, keep=True)
            return
        job = CodexJob(prompt=prompt, ws=ws, first_turn=FIRST_TURN)
        ACTIVE = job

    emit(ws, {"type": "chat", "role": "user", "text": prompt}, keep=True)
    log_event("user", text=prompt)
    gevent.spawn(job.run)


def stop_active(ws):
    with ACTIVE_LOCK:
        job = ACTIVE
    if job is None:
        emit(ws, {"type": "status", "state": "idle"}, keep=False)
        return
    gevent.spawn(job.stop)


def run_git_async(cmd, timeout):
    """Run a git command asynchronously without blocking the event loop."""
    proc = gsubprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=ROOT,
    )
    try:
        with gevent.Timeout(timeout):
            stdout, stderr = proc.communicate()
    except gevent.Timeout:
        try:
            proc.kill()
        finally:
            proc.communicate()
        raise RuntimeError(f"git command timed out after {timeout}s")

    if proc.returncode != 0:
        raise RuntimeError(stderr.strip() or "git command failed")

    return stdout.rstrip()

def run_diff(ws):
    try:
        status_text = run_git_async(["git", "-C", ROOT, "status", "--short"], 15)
        diff_text = run_git_async(["git", "-C", ROOT, "diff", "--", "."], 30)
    except (OSError, RuntimeError) as exc:
        emit(ws, {"type": "error", "text": "git diff failed: {}".format(exc)}, keep=True)
        log_event("error", text="git diff failed: {}".format(exc))
        return

    if status_text or diff_text:
        parts = []
        if status_text:
            parts.append("Status\n" + status_text)
        if diff_text:
            parts.append("Diff\n" + diff_text)
        text = "\n\n".join(parts)
    else:
        text = "No changes."
    emit(ws, {"type": "diff", "text": text}, keep=True)


def list_files():
    entries = []
    root_real = os.path.realpath(ROOT)
    for dirpath, dirnames, filenames in os.walk(root_real):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)
        rel_dir = os.path.relpath(dirpath, root_real)
        if rel_dir == ".":
            rel_dir = ""
        for filename in sorted(filenames):
            full_path = os.path.join(dirpath, filename)
            try:
                stat = os.stat(full_path)
            except OSError:
                continue
            rel_path = os.path.join(rel_dir, filename) if rel_dir else filename
            rel_path = rel_path.replace(os.sep, "/")
            entries.append({"path": rel_path, "size": stat.st_size})
    return entries


def safe_file_path(path):
    if not isinstance(path, str) or not path:
        raise ValueError("unsafe file path")
    if os.path.isabs(path):
        raise ValueError("absolute paths are not allowed")

    normalized = posixpath.normpath(path.replace("\\", "/"))
    if normalized in {"", "."} or normalized == ".." or normalized.startswith("../"):
        raise ValueError("unsafe file path")

    parts = [part for part in normalized.split("/") if part]
    if any(part in SKIP_DIRS for part in parts):
        raise ValueError("unsafe file path")

    root_real = os.path.realpath(ROOT)
    full_path = os.path.realpath(os.path.join(root_real, *parts))
    if os.path.commonpath([root_real, full_path]) != root_real:
        raise ValueError("unsafe file path")
    return full_path, "/".join(parts)


def mode_for_path(path):
    ext = os.path.splitext(path.lower())[1]
    if ext in MARKDOWN_EXTS:
        return "markdown"
    if ext in CODE_EXTS:
        return "code"
    return "text"


def read_file_for_view(path):
    full_path, rel_path = safe_file_path(path)
    if not os.path.isfile(full_path):
        raise ValueError("file not found")
    size = os.path.getsize(full_path)
    if size > MAX_FILE_SIZE:
        raise ValueError("file is too large")
    with open(full_path, "rb") as f:
        sample = f.read(8192)
        if b"\0" in sample:
            raise ValueError("binary file")
        f.seek(0)
        data = f.read()
    try:
        text = data.decode("utf-8")
        editable = True
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
        editable = False
    return {
        "type": "file",
        "path": rel_path,
        "text": text,
        "mode": mode_for_path(rel_path),
        "hash": hashlib.sha256(data).hexdigest(),
        "editable": editable,
    }


def write_file_from_view(path, text, expected_hash):
    if not isinstance(text, str):
        raise ValueError("file text must be a string")
    if not isinstance(expected_hash, str) or not expected_hash:
        raise ValueError("missing file version")

    full_path, rel_path = safe_file_path(path)
    if not os.path.isfile(full_path):
        raise ValueError("file not found")
    with open(full_path, "rb") as f:
        sample = f.read(8192)
        if b"\0" in sample:
            raise ValueError("binary file")
        f.seek(0)
        old_data = f.read()

    try:
        old_data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("file is not UTF-8 editable")

    current_hash = hashlib.sha256(old_data).hexdigest()
    if current_hash != expected_hash:
        raise ValueError("file changed on disk; reload before saving")

    new_data = text.encode("utf-8")
    if len(new_data) > MAX_FILE_SIZE:
        raise ValueError("file is too large")

    directory = os.path.dirname(full_path)
    basename = os.path.basename(full_path)
    tmp_path = os.path.join(directory, ".{}.tmp.{}.{}".format(basename, os.getpid(), time.time_ns()))
    try:
        with open(tmp_path, "wb") as f:
            f.write(new_data)
        os.replace(tmp_path, full_path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass

    return read_file_for_view(rel_path)


def clear_scrollback():
    with SCROLLBACK_LOCK:
        SCROLLBACK.clear()


def handle_ws_message(ws, raw):
    try:
        msg = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        emit(ws, {"type": "error", "text": "Malformed JSON message"}, keep=True)
        return

    if not isinstance(msg, dict):
        emit(ws, {"type": "error", "text": "Message must be a JSON object"}, keep=True)
        return

    msg_type = msg.get("type")
    if msg_type == "prompt":
        start_codex_turn(ws, msg.get("text", ""))
    elif msg_type == "stop":
        stop_active(ws)
    elif msg_type == "diff":
        gevent.spawn(run_diff, ws)
    elif msg_type == "files":
        emit(ws, {"type": "files", "files": list_files()}, keep=False)
    elif msg_type == "file":
        try:
            emit(ws, read_file_for_view(msg.get("path")), keep=False)
        except ValueError as exc:
            emit(ws, {"type": "error", "text": str(exc)}, keep=True)
    elif msg_type == "write_file":
        try:
            file_payload = write_file_from_view(msg.get("path"), msg.get("text"), msg.get("hash"))
            emit(ws, {"type": "files", "files": list_files()}, keep=False)
            emit(
                ws,
                file_payload,
                keep=False,
            )
        except ValueError as exc:
            emit(ws, {"type": "error", "text": str(exc)}, keep=True)
    elif msg_type == "clear":
        clear_scrollback()
        emit(ws, {"type": "status", "state": "cleared"}, keep=False)
    else:
        emit(ws, {"type": "error", "text": "Unknown message type: {}".format(msg_type)}, keep=True)


@APP.get("/login")
def login_get():
    if authenticated():
        return redirect_to("/")
    return login_page()


@APP.post("/login")
def login_post():
    user = request.forms.get("user", "")
    password = request.forms.get("password", "")
    if user == USER and password == PASSWORD:
        response.set_cookie(
            COOKIE_NAME,
            user,
            secret=SECRET,
            path=public_path("/"),
            httponly=True,
            samesite="Lax",
        )
        return redirect_to("/")
    response.status = 401
    return login_page("Invalid user or password.")


@APP.get("/logout")
def logout():
    response.delete_cookie(COOKIE_NAME, path=public_path("/"))
    return redirect_to("/login")


@APP.get("/")
def index():
    if not authenticated():
        return redirect_to("/login")
    return app_page()


@APP.get("/ws")
def websocket_route():
    ws = request.environ.get("wsgi.websocket")
    if ws is None:
        response.status = 400
        return "Expected WebSocket."

    if not authenticated():
        safe_send(ws, {"type": "error", "text": "Authentication required"})
        ws.close()
        return

    with SCROLLBACK_LOCK:
        replay = list(SCROLLBACK)
    for payload in replay:
        safe_send(ws, payload)

    with ACTIVE_LOCK:
        state = "running" if ACTIVE is not None and (ACTIVE.proc is None or ACTIVE.proc.poll() is None) else "idle"
    safe_send(ws, {"type": "status", "state": state})

    while True:
        try:
            raw = ws.receive()
        except WebSocketError:
            break
        if raw is None:
            break
        handle_ws_message(ws, raw)


def parse_port(value):
    try:
        port = int(value)
    except ValueError:
        raise SystemExit("error: --port must be an integer")
    if not (0 < port < 65536):
        raise SystemExit("error: --port must be between 1 and 65535")
    return port


def configure(args):
    global ROOT, BASE_PATH, USER, PASSWORD, SECRET, LOG_DIR, LOG_PATH

    ROOT = os.path.realpath(os.path.abspath(args["--root"]))
    BASE_PATH = normalize_base_path(args["--base-path"])
    USER = args["--user"]
    PASSWORD = args["--password"]
    SECRET = args["--secret"]
    LOG_DIR = os.path.join(ROOT, ".codex-web")
    LOG_PATH = os.path.join(LOG_DIR, "history.jsonl")

    git_dir = os.path.join(ROOT, ".git")
    if not os.path.isdir(git_dir):
        print("warning: workspace root should contain a .git directory: {}".format(ROOT), file=sys.stderr)


def main(argv=None):
    args = docopt(__doc__, argv=argv)
    configure(args)
    host = args["--host"]
    port = parse_port(args["--port"])
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8"):
        pass
    print("Codex Web serving {} on http://{}:{}{}".format(ROOT, host, port, public_path("/")))
    server = WSGIServer((host, port), APP, handler_class=WebSocketHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
