# Kelvin

Kelvin is a terminal chat CLI for local or OpenAI-compatible models with:

- persistent conversations on disk
- composable prompt loading
- per-directory context switching
- file injection into the system context
- tool calling through a Docker sandbox

The package entrypoint is `kelvin`.

## What It Does

A Kelvin session is tied to:

- a current working directory
- a current conversation
- a selected model/endpoint
- zero or more active prompts

Conversation history is stored on disk under `~/.kelvin.d`, while per-project pointers and injected-file config live under `.kelvin/` in the active context.

## Install

Editable install from the repo root:

```bash
uv pip install -e .
```

or:

```bash
python3 -m pip install -e .
```

Then run:

```bash
kelvin
```

## Requirements

- Python 3.11+
- Docker
- an OpenAI-compatible endpoint

Default model config points at local Ollama:

- endpoint: `ollama`
- URL: `http://localhost:11434/v1`
- model: `firmen102/qwen3.5-27b`

OpenAI is also supported through `~/.kelvin.d/config.yaml`.

## First Run

On first run Kelvin creates:

- `~/.kelvin.d/config.yaml`
- `~/.kelvin.d/convos/`
- `~/.kelvin.d/prompts/`
- `~/.kelvin.d/cli-history`

Packaged prompts are copied from `src/kelvin/prompts/` into `~/.kelvin.d/prompts/` the first time the prompt directory is initialized.

## Config

Main config file:

```text
~/.kelvin.d/config.yaml
```

Important settings:

- `default_model`
- `default_endpoint`
- `endpoints`
- `stream`
- `auto_inject_makefile`
- `restore_last_convo`
- `max_tool_iterations`

Example:

```yaml
default_model: firmen102/qwen3.5-27b
default_endpoint: ollama
stream: true
max_tool_iterations: 15

endpoints:
  ollama:
    url: http://localhost:11434/v1
    key_env: dummy
  openai:
    url: https://api.openai.com/v1
    key_env: OPENAI_API_KEY
```

## Sandbox

Kelvin exposes one tool to the model:

- `shell`

That tool runs commands in a Docker container named `sandbox`.

Context changes restart the sandbox and bind-mount the current project directory into it. The helper scripts and image assets live under:

- [sandbox/Makefile](/Users/val/s2/kelvin/sandbox/Makefile)
- [sandbox/run_sandbox.sh](/Users/val/s2/kelvin/sandbox/run_sandbox.sh)
- [sandbox/exec_sandbox.sh](/Users/val/s2/kelvin/sandbox/exec_sandbox.sh)
- [src/kelvin/Dockerfile](/Users/val/s2/kelvin/src/kelvin/Dockerfile)

If Docker is unavailable, tool calls will fail.

## Prompts

Prompts are stored in:

- [src/kelvin/prompts](/Users/val/s2/kelvin/src/kelvin/prompts)

They are organized as:

- `system/`
- `templates/`
- `workflows/`

At runtime, Kelvin loads prompts from:

- `~/.kelvin.d/prompts`

## Commands

Conversation:

- `/convo`
- `/convo list`
- `/convo new [name]`

Navigation:

- `/switch`
- `/switch list`
- `/switch <path>`

Prompts:

- `/prompts`
- `/prompt add <name>`
- `/prompt drop <name>`

Injection:

- `/inject <file>`
- `/inject list`
- `/inject drop <file>`
- `/inject clear`

Model:

- `/model`
- `/model list`
- `/model <name>`
- `/model <endpoint>:<name>`

Info:

- `/show config`
- `/show status`
- `/show history`
- `/help`
- `/quit`

Shell passthrough:

- `!<cmd>`

## Context and Storage

Global state:

- `~/.kelvin.d/chat_state.json`
- `~/.kelvin.d/convos/`
- `~/.kelvin.d/prompts/`

Per-context state:

- `.kelvin/context_state.json`
- `.kelvin/local.injected.txt`

Kelvin also auto-injects `Makefile` from the current context when `auto_inject_makefile` is enabled.

## Streaming and Paging

Kelvin streams assistant text in chunks. The CLI renders structured events internally and pages output through:

```text
less -RX
```

unless `PAGER` is already set.

Useful overrides:

```bash
PAGER=cat kelvin
PAGER='less -RX' kelvin
```

## Development Notes

This repo currently uses a `src/` layout and ships prompts as package data via:

- [pyproject.toml](/Users/val/s2/kelvin/pyproject.toml)

Useful files:

- [src/kelvin/cli.py](/Users/val/s2/kelvin/src/kelvin/cli.py)
- [src/kelvin/core.py](/Users/val/s2/kelvin/src/kelvin/core.py)
- [src/kelvin/oai.py](/Users/val/s2/kelvin/src/kelvin/oai.py)
- [src/kelvin/storage/home.py](/Users/val/s2/kelvin/src/kelvin/storage/home.py)

## Uninstall

If installed with `pip`:

```bash
python3 -m pip uninstall kelvin
```

If you also want to remove local state:

```bash
rm -rf ~/.kelvin.d
```
