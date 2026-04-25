#!/usr/bin/env python3
"""
llmore: tiny OpenAI chat client with a streaming pager.

Usage:
  llmore [options]

Options:
  -m MODEL --model=MODEL   Model name [env: LLMORE_MODEL, default: gpt-5.2].
  -u URL --base-url=URL    OpenAI-compatible base URL [env: LLMORE_BASE_URL, default: http://n:11434/v1].
  --api-key=KEY            API key [env: OPENAI_API_KEY, default: ollama].
  --list-models            List available models and exit.
  --no-tools               Disable model tool calling.
  --text-tools             Use XML/JSON text tool protocol instead of native tool calls.
  --show-chunks            Print a middle dot after each streamed text chunk.
  --log-file=PATH          Write debug logs to PATH; use --log-file= to disable [default: llmore.log].
  -s SYS --system=SYS      Developer/system instruction.
  --system-dir=DIR         Read sorted files from DIR as additional system instruction.
  -r N --rows=N            Override terminal rows.
  -h --help                Show this help.

Keys while paging:
  space     next page
  enter     next line
  q         quit pager; stream rest directly
  Ctrl-C    cancel current answer
  Ctrl-D    exit chat

Keys while editing:
  enter     submit prompt
  \         enter paste mode
  Ctrl-J    submit paste
  Esc-enter submit paste
"""

###
### imports / constants
###

import fnmatch, json, logging, os, re, subprocess, sys, tty, termios, threading, queue
from collections import deque
from contextlib import contextmanager

from docopt import docopt
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from wcwidth import wcwidth
from openai import APIConnectionError, NotFoundError, OpenAI, OpenAIError

TAB = 8
HIST = os.path.expanduser("~/.llmore_history")
#DEFAULT_MODEL = "gpt-5.2"
DEFAULT_MODEL = "qwen3:14b"
DEFAULT_BASE_URL = "http://n:11434/v1"
TOOL_BUDGET = 4
TOOL_TIMEOUT = 60
TOOLS_ENABLED = True
TEXT_TOOLS_ENABLED = False
SHOW_CHUNKS = False
LOG = logging.getLogger("llmore")

ANSI = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[PX^_].*?\x1b\\|[@-Z\\-_])",
    re.S,
)
EXECUTE = re.compile(
    r"<execute\b(?P<attrs>[^>]*)>(?P<command>.*?)</execute>",
    re.I | re.S,
)
ATTR = re.compile(r"""(\w+)\s*=\s*(['"])(.*?)\2""", re.S)
TEXT_TOOL_HEADER = "You are running inside llmore. llmore may provide a text tool protocol for shell access."
TEXT_TOOL_FOOTER = """Runtime tool protocol for llmore:

You are a programming assistant with access to the local working directory through text tools.

Available text tools:
- shell: run a simple, noninteractive shell command and capture stdout/stderr
- write_file: create or replace a file
- read_files: read one or more files, optionally with line ranges
- search_replace: replace exact text in a file
- apply_patch: apply a unified diff patch
- list_files: list files by directory/pattern
- run_live: run a command attached to the terminal without capturing output

Use text tools whenever you need to inspect local state or perform a local side effect. If the user asks you to create or modify files, do not merely show code for the user to copy. Use a tool, then summarize what changed.

write_file request:
<execute tool="write_file" path="relative/path.py" purpose="short reason">
file contents
</execute>

read_files request, one file per line. Forms: path, path:start-end, path:start-, path:-end.
<execute tool="read_files" purpose="short reason">
relative/path.py
another/file.txt:10-40
</execute>

search_replace request:
<execute tool="search_replace" path="relative/path.py" purpose="short reason">
<search>
old exact text
</search>
<replace>
new exact text
</replace>
</execute>

apply_patch request:
<execute tool="apply_patch" purpose="short reason">
unified diff patch accepted by git apply
</execute>

list_files request:
<execute tool="list_files" path="." pattern="*.py" recursive="true" purpose="short reason">
</execute>

shell request:
<execute tool="shell" purpose="short reason">
command
</execute>

run_live request:
<execute tool="run_live" purpose="short reason">
interactive or live command
</execute>

Rules:
- Do not use Markdown fences for tool requests.
- Do not output more than one <execute> block at a time.
- Do not answer the user in the same message as an <execute> block.
- After llmore replies with <response>, answer normally in Markdown.
- If more work is needed after a <response>, you may emit one more <execute> block and stop again.
- If no command is needed, answer normally in Markdown.
- Prefer write_file for file creation/replacement.
- Prefer search_replace for small exact edits.
- Prefer apply_patch for multi-location or structured edits.
- Prefer list_files for file discovery.
- Prefer read_files for reading known files.
- Prefer shell for listing, searching, and captured command output.
- Prefer run_live only when the user should directly see or interact with command output.

llmore replies with JSON:

<response tool="tool_name">
{"success": true, "...": "..."}
</response>"""


###
### prompt
###

def prompt_sessions():
    kb = KeyBindings()

    @kb.add("c-j")
    @kb.add("escape", "enter")
    def _(event):
        event.current_buffer.validate_and_handle()

    history = FileHistory(HIST)
    return (
        PromptSession(history=history),
        PromptSession(history=history, key_bindings=kb, multiline=True),
    )

def read_system_dir(path):
    parts = []

    for name in sorted(os.listdir(path)):
        file_path = os.path.join(path, name)

        if not os.path.isfile(file_path):
            continue

        with open(file_path, encoding="utf-8") as f:
            parts.append(f"### {name}\n{f.read()}")

    return "\n\n".join(parts)

def build_system_prompt(system, system_dir):
    parts = []

    if system:
        parts.append(system)

    if system_dir:
        parts.append(read_system_dir(system_dir))

    return "\n\n".join(p for p in parts if p)

def add_system_text(system, text):
    return "\n\n".join(p for p in (system, text) if p)

def setup_logging(path):
    if not path:
        logging.basicConfig(handlers=[logging.NullHandler()])
        return

    logging.basicConfig(
        filename=path,
        filemode="a",
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    LOG.info("logging started")


###
### terminal / ansi helpers
###

def cw(c):
    return max(wcwidth(c), 0)

def toks(s):
    p = 0
    for m in ANSI.finditer(s):
        if m.start() > p:
            yield False, s[p:m.start()]
        yield True, m.group(0)
        p = m.end()
    if p < len(s):
        yield False, s[p:]

def term_size(row_override=None):
    for f in (sys.stdout, sys.stderr, sys.stdin):
        try:
            z = os.get_terminal_size(f.fileno())
            return max(z.columns, 1), max(int(row_override or z.lines), 2)
        except OSError:
            pass
    return int(os.getenv("COLUMNS", 80)), max(int(row_override or os.getenv("LINES", 24)), 2)

@contextmanager
def cbreak():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

def pager_cmd():
    p = "--More--"
    sys.stderr.write(p)
    sys.stderr.flush()

    try:
        with cbreak():
            c = sys.stdin.read(1)
    finally:
        sys.stderr.write("\r" + " " * len(p) + "\r")
        sys.stderr.flush()

    return {
        " ": "page",
        "\n": "line",
        "\r": "line",
        "q": "quit",
        "Q": "quit",
    }.get(c, "unknown")

def pause_cmd(prompt):
    p = f"{prompt} [y/N] "
    sys.stderr.write(p)
    sys.stderr.flush()

    try:
        with cbreak():
            c = sys.stdin.read(1)
    finally:
        sys.stderr.write("\r" + " " * len(p) + "\r")
        sys.stderr.flush()

    return "continue" if c in ("y", "Y") else "cancel"


###
### pager
###

class Pager:
    def __init__(self, cols, rows):
        self.q = deque()
        self.row = []
        self.col = self.used = 0
        self.paused = False
        self.paging = True
        self.resize(cols, rows)

    def resize(self, cols, rows):
        self.cols = max(cols, 1)
        self.height = max(rows - 1, 1)

    def feed(self, s):
        if not self.paging:
            return s

        for ansi, text in toks(s):
            if ansi:
                self.row.append(text)
            else:
                for c in text:
                    self.put(c)

        return self.drain()

    def put(self, c):
        if c == "\n":
            self.row.append(c)
            self.flush()
            self.col = 0
            return

        if c == "\t":
            for _ in range(TAB - self.col % TAB):
                self.put(" ")
            return

        w = cw(c)

        if not w:
            self.row.append(c)
            return

        if self.col + w > self.cols:
            self.flush()
            self.col = 0

        self.row.append(c)
        self.col += w

        # Be conservative when wrapping at the terminal edge.
        if self.col == self.cols:
            self.flush()
            self.col = 0

    def flush(self):
        if self.row:
            self.q.append("".join(self.row))
            self.row = []

    def drain(self):
        if self.paused or not self.paging:
            return ""

        out = []

        while self.q:
            if self.used >= self.height:
                self.paused = True
                break

            out.append(self.q.popleft())
            self.used += 1

        return "".join(out)

    def page(self):
        self.used = 0
        self.paused = False
        return self.drain()

    def line(self):
        self.used = max(self.height - 1, 0)
        self.paused = False
        return self.drain()

    def quit_pager(self):
        self.paging = False
        self.paused = False

        self.flush()
        out = "".join(self.q)
        self.q.clear()

        return out

    def close(self):
        self.flush()
        return self.drain() if self.paging else ""


###
### output driver
###

def emit(s):
    if s:
        print(s, end="", flush=True)

def drive(pager, row_override=None):
    while pager.paused:
        pager.resize(*term_size(row_override))
        c = pager_cmd()

        if c == "quit":
            emit(pager.quit_pager())
            return

        if c == "page":
            emit(pager.page())
        elif c == "line":
            emit(pager.line())


###
### OpenAI streaming
###

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "shell",
            "description": "Run a shell command line in the current working directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command_line": {
                        "type": "string",
                        "description": "The shell command line to execute.",
                    },
                },
                "required": ["command_line"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or replace a UTF-8 text file in the current working directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to write."},
                    "content": {"type": "string", "description": "Complete file contents."},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_files",
            "description": "Read UTF-8 text files from the current working directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {
                            "oneOf": [
                                {"type": "string"},
                                {
                                    "type": "object",
                                    "properties": {
                                        "path": {"type": "string"},
                                        "start_line": {"type": "integer"},
                                        "end_line": {"type": "integer"},
                                    },
                                    "required": ["path"],
                                    "additionalProperties": False,
                                },
                            ],
                        },
                    },
                },
                "required": ["paths"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_replace",
            "description": "Replace exact text in a UTF-8 text file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to edit."},
                    "search": {"type": "string", "description": "Exact text to replace."},
                    "replace": {"type": "string", "description": "Replacement text."},
                    "count": {"type": "integer", "description": "Number of replacements; defaults to 1."},
                },
                "required": ["path", "search", "replace"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_live",
            "description": "Run a command attached to the terminal without capturing stdout/stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command_line": {"type": "string", "description": "The command line to execute."},
                },
                "required": ["command_line"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "Apply a unified diff patch using git apply after checking it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "patch": {"type": "string", "description": "Unified diff patch accepted by git apply."},
                },
                "required": ["patch"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in the current working directory with optional pattern and recursion.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative directory to list. Defaults to ."},
                    "pattern": {"type": "string", "description": "Filename glob pattern. Defaults to *."},
                    "recursive": {"type": "boolean", "description": "Whether to recurse into subdirectories."},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
]


def run_shell(command_line):
    LOG.debug("shell start command=%r", command_line)
    try:
        p = subprocess.run(
            command_line,
            shell=True,
            cwd=os.getcwd(),
            text=True,
            capture_output=True,
            timeout=TOOL_TIMEOUT,
        )
        result = {
            "success": p.returncode == 0,
            "exitcode": p.returncode,
            "stdout": p.stdout,
            "stderr": p.stderr,
        }
    except subprocess.TimeoutExpired as e:
        result = {
            "success": False,
            "exitcode": None,
            "stdout": e.stdout or "",
            "stderr": f"Command timed out after {TOOL_TIMEOUT} seconds.",
        }
    except Exception as e:
        result = {
            "success": False,
            "exitcode": None,
            "stdout": "",
            "stderr": str(e),
        }

    LOG.debug("shell result %s", json.dumps(result))
    return result


def run_live(command_line):
    LOG.debug("run_live start command=%r", command_line)
    try:
        p = subprocess.run(
            command_line,
            shell=True,
            cwd=os.getcwd(),
            timeout=TOOL_TIMEOUT,
        )
        result = {"success": p.returncode == 0, "exitcode": p.returncode, "stderr": ""}
    except subprocess.TimeoutExpired:
        result = {
            "success": False,
            "exitcode": None,
            "stderr": f"Command timed out after {TOOL_TIMEOUT} seconds.",
        }
    except Exception as e:
        result = {"success": False, "exitcode": None, "stderr": str(e)}

    LOG.debug("run_live result %s", json.dumps(result))
    return result


def work_path(path):
    root = os.getcwd()
    if not path:
        raise ValueError("path is required")
    if os.path.isabs(path):
        raise ValueError("path must be relative")

    full = os.path.abspath(os.path.join(root, path))
    if os.path.commonpath([root, full]) != root:
        raise ValueError("path must stay inside the current working directory")

    return full


def write_file(path, content):
    LOG.debug("write_file start path=%r bytes=%d", path, len(content or ""))
    try:
        full = work_path(path)
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content or "")
        result = {
            "success": True,
            "path": path,
            "bytes": len((content or "").encode("utf-8")),
            "stderr": "",
        }
    except Exception as e:
        result = {"success": False, "path": path, "bytes": 0, "stderr": str(e)}

    LOG.debug("write_file result %s", json.dumps(result))
    return result


def parse_file_spec(spec):
    if isinstance(spec, dict):
        return {
            "path": spec.get("path") or "",
            "start_line": spec.get("start_line"),
            "end_line": spec.get("end_line"),
        }

    text = str(spec)
    m = re.match(r"^(?P<path>.+):(?P<start>\d*)-(?P<end>\d*)$", text)
    if not m:
        return {"path": text, "start_line": None, "end_line": None}

    start = m.group("start")
    end = m.group("end")
    return {
        "path": m.group("path"),
        "start_line": int(start) if start else None,
        "end_line": int(end) if end else None,
    }


def slice_lines(content, start_line, end_line):
    if start_line is None and end_line is None:
        return content

    lines = content.splitlines(keepends=True)
    start = max((start_line or 1) - 1, 0)
    end = end_line if end_line is not None else len(lines)
    return "".join(lines[start:end])


def read_files(paths):
    LOG.debug("read_files start paths=%s", json.dumps(paths))
    files = []
    ok = True

    for spec in paths or []:
        parsed = parse_file_spec(spec)
        path = parsed["path"]
        start_line = parsed["start_line"]
        end_line = parsed["end_line"]
        try:
            with open(work_path(path), encoding="utf-8") as f:
                content = slice_lines(f.read(), start_line, end_line)
            files.append({
                "success": True,
                "path": path,
                "start_line": start_line,
                "end_line": end_line,
                "content": content,
                "stderr": "",
            })
        except Exception as e:
            ok = False
            files.append({
                "success": False,
                "path": path,
                "start_line": start_line,
                "end_line": end_line,
                "content": "",
                "stderr": str(e),
            })

    result = {"success": ok, "files": files}
    LOG.debug("read_files result %s", json.dumps(result))
    return result


def list_files(path=".", pattern="*", recursive=False):
    LOG.debug("list_files start path=%r pattern=%r recursive=%r", path, pattern, recursive)
    try:
        root = work_path(path or ".")
        if not os.path.isdir(root):
            raise ValueError("path must be a directory")

        base = os.getcwd()
        files = []
        if recursive:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
                for name in filenames:
                    if fnmatch.fnmatch(name, pattern or "*"):
                        files.append(os.path.relpath(os.path.join(dirpath, name), base))
        else:
            for name in sorted(os.listdir(root)):
                full = os.path.join(root, name)
                if os.path.isfile(full) and fnmatch.fnmatch(name, pattern or "*"):
                    files.append(os.path.relpath(full, base))

        result = {"success": True, "files": sorted(files), "stderr": ""}
    except Exception as e:
        result = {"success": False, "files": [], "stderr": str(e)}

    LOG.debug("list_files result %s", json.dumps(result))
    return result


def apply_patch_tool(patch):
    LOG.debug("apply_patch start bytes=%d", len(patch or ""))
    if not patch:
        result = {"success": False, "exitcode": None, "stdout": "", "stderr": "patch is required"}
        LOG.debug("apply_patch result %s", json.dumps(result))
        return result

    check = subprocess.run(
        ["git", "apply", "--check", "--whitespace=nowarn"],
        cwd=os.getcwd(),
        input=patch,
        text=True,
        capture_output=True,
    )
    if check.returncode != 0:
        result = {
            "success": False,
            "exitcode": check.returncode,
            "stdout": check.stdout,
            "stderr": check.stderr,
        }
        LOG.debug("apply_patch result %s", json.dumps(result))
        return result

    applied = subprocess.run(
        ["git", "apply", "--whitespace=nowarn"],
        cwd=os.getcwd(),
        input=patch,
        text=True,
        capture_output=True,
    )
    result = {
        "success": applied.returncode == 0,
        "exitcode": applied.returncode,
        "stdout": applied.stdout,
        "stderr": applied.stderr,
    }
    LOG.debug("apply_patch result %s", json.dumps(result))
    return result


def search_replace(path, search, replace, count=1):
    LOG.debug("search_replace start path=%r count=%r", path, count)
    try:
        full = work_path(path)
        with open(full, encoding="utf-8") as f:
            content = f.read()

        if not search:
            raise ValueError("search text is required")

        matches = content.count(search)
        if matches == 0:
            raise ValueError("search text not found")

        n = int(count if count is not None else 1)
        if n < 1:
            raise ValueError("count must be at least 1")

        new_content = content.replace(search, replace or "", n)
        replacements = min(matches, n)
        with open(full, "w", encoding="utf-8") as f:
            f.write(new_content)

        result = {
            "success": True,
            "path": path,
            "replacements": replacements,
            "stderr": "",
        }
    except Exception as e:
        result = {"success": False, "path": path, "replacements": 0, "stderr": str(e)}

    LOG.debug("search_replace result %s", json.dumps(result))
    return result


def parse_attrs(s):
    return {m.group(1).lower(): m.group(3) for m in ATTR.finditer(s)}


def find_execute(content):
    m = EXECUTE.search(content)
    if not m:
        return None
    attrs = parse_attrs(m.group("attrs"))
    return {
        "tool": attrs.get("tool", ""),
        "purpose": attrs.get("purpose", ""),
        "path": attrs.get("path", ""),
        "pattern": attrs.get("pattern", ""),
        "recursive": attrs.get("recursive", ""),
        "command": m.group("command").strip(),
    }


def tag_text(content, tag):
    m = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", content, re.I | re.S)
    return m.group(1) if m else ""


def format_response(tool, result):
    return (
        f'<response tool="{tool}">\n'
        f"{json.dumps(result)}\n"
        "</response>"
    )


def filter_text_tool_display(chunk, state):
    if state.get("suppressing"):
        return ""

    start = chunk.lower().find("<execute")
    if start < 0:
        return chunk

    state["suppressing"] = True
    return chunk[:start]


def execute_tool(call):
    name = call.get("function", {}).get("name")
    arguments = call.get("function", {}).get("arguments") or "{}"

    try:
        args = json.loads(arguments)
    except json.JSONDecodeError as e:
        return {
            "success": False,
            "exitcode": None,
            "stdout": "",
            "stderr": f"Invalid tool arguments JSON: {e}",
        }

    if not isinstance(args, dict):
        return {
            "success": False,
            "exitcode": None,
            "stdout": "",
            "stderr": "Tool arguments must be a JSON object.",
        }

    if name == "shell":
        return run_shell(args.get("command_line") or "")

    if name == "run_live":
        return run_live(args.get("command_line") or "")

    if name == "write_file":
        return write_file(args.get("path") or "", args.get("content") or "")

    if name == "read_files":
        paths = args.get("paths") or []
        if not isinstance(paths, list):
            paths = []
        return read_files(paths)

    if name == "list_files":
        return list_files(
            args.get("path") or ".",
            args.get("pattern") or "*",
            bool(args.get("recursive", False)),
        )

    if name == "apply_patch":
        return apply_patch_tool(args.get("patch") or "")

    if name == "search_replace":
        return search_replace(
            args.get("path") or "",
            args.get("search") or "",
            args.get("replace") or "",
            args.get("count", 1),
        )

    return {
        "success": False,
        "exitcode": None,
        "stdout": "",
        "stderr": f"Unknown tool: {name}",
    }


def stream_turn(client, model, messages, cancel=None):
    kwargs = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    if TOOLS_ENABLED:
        kwargs["tools"] = TOOLS

    stream = client.chat.completions.create(**kwargs)
    content = []
    tool_calls = {}

    for chunk in stream:
        if cancel and cancel.is_set():
            return None

        if not chunk.choices:
            continue

        choice = chunk.choices[0]
        delta = choice.delta
        text = getattr(delta, "content", None)

        if text:
            LOG.debug("chunk text=%r", text)
            content.append(text)
            yield {"role": "display", "content": text}
            if SHOW_CHUNKS:
                yield {"role": "display", "content": "·"}

        for tool_call in getattr(delta, "tool_calls", None) or []:
            LOG.debug("chunk tool_call=%r", tool_call)
            index = getattr(tool_call, "index", 0)
            call = tool_calls.setdefault(index, {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            })

            call_id = getattr(tool_call, "id", None)
            if call_id:
                call["id"] = call_id

            call_type = getattr(tool_call, "type", None)
            if call_type:
                call["type"] = call_type

            fn = getattr(tool_call, "function", None)
            if fn:
                name = getattr(fn, "name", None)
                if name:
                    call["function"]["name"] += name

                arguments = getattr(fn, "arguments", None)
                if arguments:
                    call["function"]["arguments"] += arguments

    calls = [tool_calls[i] for i in sorted(tool_calls)]
    LOG.debug("turn done content=%r tool_calls=%s", "".join(content), json.dumps(calls))
    yield {
        "role": "turn_done",
        "content": "".join(content),
        "tool_calls": calls,
    }


def run_answer(client, model, messages, ui_events, control_events, cancel):
    final = ""
    tool_count = 0

    try:
        while not cancel.is_set():
            turn = None
            display_state = {}

            for event in stream_turn(client, model, messages, cancel):
                if cancel.is_set():
                    ui_events.put({"role": "done", "content": None})
                    return

                if event.get("role") == "display":
                    if TEXT_TOOLS_ENABLED:
                        content = filter_text_tool_display(event.get("content", ""), display_state)
                        if content:
                            ui_events.put({"role": "display", "content": content})
                        continue

                    ui_events.put(event)
                    continue

                if event.get("role") == "turn_done":
                    turn = event

            if turn is None:
                ui_events.put({"role": "done", "content": None})
                return

            content = turn.get("content") or ""
            tool_calls = turn.get("tool_calls") or []

            if not tool_calls:
                request = find_execute(content) if TEXT_TOOLS_ENABLED else None
                if request:
                    LOG.debug("text tool request %s", json.dumps(request))
                    messages.append({"role": "assistant", "content": content})
                    tool = request.get("tool")
                    command = request.get("command")
                    path = request.get("path")
                    pattern = request.get("pattern") or "*"
                    recursive = str(request.get("recursive")).lower() in ("1", "true", "yes")

                    if tool_count >= TOOL_BUDGET:
                        ok = pause_for_input(
                            ui_events,
                            control_events,
                            f"tool-budget-{tool_count}",
                            "tool_budget_exceeded",
                            "Tool budget exceeded. Continue?",
                            cancel,
                        )
                        if not ok:
                            ui_events.put({"role": "error", "content": "Stopped: tool budget exceeded.\n"})
                            ui_events.put({"role": "done", "content": None})
                            return

                        tool_count = 0

                    tool_count += 1
                    purpose = request.get("purpose")
                    if purpose:
                        ui_events.put({
                            "role": "display",
                            "content": f"\n[tool request] {purpose}\n",
                        })

                    if tool == "shell":
                        ui_events.put({"role": "display", "content": f"\n[shell] $ {command}\n"})
                        result = run_shell(command)
                    elif tool == "run_live":
                        ui_events.put({"role": "display", "content": f"\n[run_live] $ {command}\n"})
                        result = run_live(command)
                    elif tool == "write_file":
                        ui_events.put({"role": "display", "content": f"\n[write_file] {path}\n"})
                        result = write_file(path, command)
                    elif tool == "read_files":
                        paths = [p.strip() for p in command.splitlines() if p.strip()]
                        ui_events.put({"role": "display", "content": f"\n[read_files] {', '.join(paths)}\n"})
                        result = read_files(paths)
                    elif tool == "list_files":
                        ui_events.put({"role": "display", "content": f"\n[list_files] {path or '.'} {pattern}\n"})
                        result = list_files(path or ".", pattern, recursive)
                    elif tool == "apply_patch":
                        ui_events.put({"role": "display", "content": "\n[apply_patch]\n"})
                        result = apply_patch_tool(command)
                    elif tool == "search_replace":
                        ui_events.put({"role": "display", "content": f"\n[search_replace] {path}\n"})
                        result = search_replace(path, tag_text(command, "search"), tag_text(command, "replace"))
                    else:
                        result = {
                            "success": False,
                            "tool": tool,
                            "stderr": f"Unknown text tool: {tool}",
                        }

                    ui_events.put({
                        "role": "display",
                        "content": f"[{tool or 'tool'} {'ok' if result.get('success') else 'failed'}]\n",
                    })
                    messages.append({"role": "user", "content": format_response(tool or "tool", result)})
                    continue

                messages.append({"role": "assistant", "content": content})
                final = content
                LOG.debug("answer done content=%r", final)
                ui_events.put({"role": "done", "content": final})
                return

            for i, call in enumerate(tool_calls, 1):
                if not call.get("id"):
                    call["id"] = f"tool-call-{tool_count + i}"

            assistant = {
                "role": "assistant",
                "content": content or None,
                "tool_calls": tool_calls,
            }
            messages.append(assistant)
            LOG.debug("native tool calls %s", json.dumps(tool_calls))

            for call in tool_calls:
                if cancel.is_set():
                    ui_events.put({"role": "done", "content": None})
                    return

                if tool_count >= TOOL_BUDGET:
                    ok = pause_for_input(
                        ui_events,
                        control_events,
                        f"tool-budget-{tool_count}",
                        "tool_budget_exceeded",
                        "Tool budget exceeded. Continue?",
                        cancel,
                    )
                    if not ok:
                        ui_events.put({"role": "error", "content": "Stopped: tool budget exceeded.\n"})
                        ui_events.put({"role": "done", "content": None})
                        return

                    tool_count = 0

                tool_count += 1
                name = call.get("function", {}).get("name", "")
                arguments = call.get("function", {}).get("arguments") or "{}"

                try:
                    args = json.loads(arguments)
                except json.JSONDecodeError:
                    args = {}
                if not isinstance(args, dict):
                    args = {}

                if name == "shell":
                    ui_events.put({
                        "role": "display",
                        "content": f"\n[shell] $ {args.get('command_line', '')}\n",
                    })
                elif name == "run_live":
                    ui_events.put({
                        "role": "display",
                        "content": f"\n[run_live] $ {args.get('command_line', '')}\n",
                    })
                elif name == "write_file":
                    ui_events.put({
                        "role": "display",
                        "content": f"\n[write_file] {args.get('path', '')}\n",
                    })
                elif name == "read_files":
                    paths = args.get("paths") if isinstance(args.get("paths"), list) else []
                    names = [p.get("path", "") if isinstance(p, dict) else str(p) for p in paths]
                    ui_events.put({
                        "role": "display",
                        "content": f"\n[read_files] {', '.join(names)}\n",
                    })
                elif name == "list_files":
                    ui_events.put({
                        "role": "display",
                        "content": f"\n[list_files] {args.get('path', '.')} {args.get('pattern', '*')}\n",
                    })
                elif name == "apply_patch":
                    ui_events.put({
                        "role": "display",
                        "content": "\n[apply_patch]\n",
                    })
                elif name == "search_replace":
                    ui_events.put({
                        "role": "display",
                        "content": f"\n[search_replace] {args.get('path', '')}\n",
                    })

                result = execute_tool(call)
                ui_events.put({
                    "role": "display",
                    "content": f"[{name or 'tool'} {'ok' if result.get('success') else 'failed'}]\n",
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "name": name,
                    "content": json.dumps(result),
                })

    except NotFoundError as e:
        ui_events.put({
            "role": "error",
            "content": (
                f"Model not found: {model}\n"
                "Run `llmore --list-models` or pass an installed model with `llmore -m MODEL`.\n"
                f"Server said: {e}\n"
            ),
        })
        ui_events.put({"role": "done", "content": None})
    except APIConnectionError as e:
        ui_events.put({"role": "error", "content": f"Could not reach model server: {e}\n"})
        ui_events.put({"role": "done", "content": None})
    except OpenAIError as e:
        ui_events.put({"role": "error", "content": f"OpenAI-compatible API error: {e}\n"})
        ui_events.put({"role": "done", "content": None})
    except Exception as e:
        ui_events.put({"role": "error", "content": f"Unexpected worker error: {e}\n"})
        ui_events.put({"role": "done", "content": None})


def pause_for_input(ui_events, control_events, pause_id, reason, content, cancel):
    ui_events.put({
        "role": "pause",
        "id": pause_id,
        "reason": reason,
        "content": content,
    })

    while not cancel.is_set():
        try:
            event = control_events.get(timeout=0.1)
        except queue.Empty:
            continue

        if event.get("role") != "resume":
            continue

        if event.get("id") != pause_id:
            continue

        return event.get("content") == "continue"

    return False


def print_models(client):
    try:
        models = client.models.list()
    except APIConnectionError as e:
        print(f"Could not reach model server: {e}", file=sys.stderr)
        return 1
    except OpenAIError as e:
        print(f"Could not list models: {e}", file=sys.stderr)
        return 1

    for model in models.data:
        print(model.id)

    return 0


def answer(client, model, messages, row_override=None):
    pager = Pager(*term_size(row_override))
    ui_events = queue.Queue()
    control_events = queue.Queue()
    cancel = threading.Event()
    worker = threading.Thread(
        target=run_answer,
        args=(client, model, messages, ui_events, control_events, cancel),
        daemon=True,
    )
    worker.start()

    try:
        while True:
            event = ui_events.get()
            role = event.get("role")

            if role == "display":
                emit(pager.feed(event.get("content", "")))
                drive(pager, row_override)
                continue

            if role == "error":
                emit(pager.feed(event.get("content", "")))
                drive(pager, row_override)
                continue

            if role == "pause":
                emit(pager.close())
                action = pause_cmd(event.get("content", "Continue?"))
                control_events.put({
                    "role": "resume",
                    "id": event.get("id"),
                    "content": action,
                })

                if action == "cancel":
                    cancel.set()
                continue

            if role == "done":
                emit(pager.close())
                worker.join(timeout=1)
                return event.get("content")

    except KeyboardInterrupt:
        cancel.set()
        worker.join(timeout=1)
        print("\n^C")
        return None


###
### chat loop
###

def chat(client, model, system, row_override=None):
    single_prompt, paste_prompt = prompt_sessions()

    messages = []

    if system:
        messages.append({"role": "system", "content": system})

    print("llmore chat")
    print("Pager: space=page, enter=line, q=quit pager/pass-through, Ctrl-C=cancel answer, Ctrl-D=exit.\n")
    print("Prompt: enter=submit, \\=paste mode, Ctrl-J/Esc-enter=submit paste.\n")

    while True:
        try:
            prompt = single_prompt.prompt("chat> ")
            if prompt == "\\":
                prompt = paste_prompt.prompt("paste> ")
        except EOFError:
            print()
            return
        except KeyboardInterrupt:
            print()
            continue

        text = prompt.strip()

        if not text:
            continue

        if text in ("q", "quit", "exit", ":q"):
            return

        LOG.debug("user prompt=%r", prompt)
        turn_start = len(messages)
        messages.append({"role": "user", "content": prompt})
        print()

        reply = answer(client, model, messages, row_override)
        print()

        if reply is None:
            del messages[turn_start:]
            continue


###
### main
###

def main():
    global TOOLS_ENABLED, TEXT_TOOLS_ENABLED, SHOW_CHUNKS
    a = docopt(__doc__)
    setup_logging(a["--log-file"])
    if a["--no-tools"] and a["--text-tools"]:
        print("Use only one of --no-tools or --text-tools.", file=sys.stderr)
        raise SystemExit(1)

    TEXT_TOOLS_ENABLED = bool(a["--text-tools"])
    TOOLS_ENABLED = not a["--no-tools"] and not TEXT_TOOLS_ENABLED
    SHOW_CHUNKS = bool(a["--show-chunks"])
    model = a["--model"] or os.getenv("LLMORE_MODEL") or DEFAULT_MODEL
    base_url = a["--base-url"] or os.getenv("LLMORE_BASE_URL") or DEFAULT_BASE_URL
    api_key = a["--api-key"] or os.getenv("OPENAI_API_KEY") or "ollama"
    rows = int(a["--rows"]) if a["--rows"] else None
    client = OpenAI(base_url=base_url, api_key=api_key)

    if a["--list-models"]:
        raise SystemExit(print_models(client))

    try:
        system = build_system_prompt(a["--system"], a["--system-dir"])
    except OSError as e:
        print(f"Could not read system directory: {e}", file=sys.stderr)
        raise SystemExit(1)

    if TEXT_TOOLS_ENABLED:
        system = "\n\n".join(p for p in (TEXT_TOOL_HEADER, system, TEXT_TOOL_FOOTER) if p)

    chat(client, model, system, rows)

if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
