#!/usr/bin/env python3
"""
Lab Infra Chat CLI

Usage:
  chat.py [--config=<path>] [--help] [--version]

Options:
  --config=<path>    Path to configuration file [default: infra/config/chat.yaml]
  --help             Show help message
  --version          Show version
"""

import os
import shlex
import subprocess
from pathlib import Path

from docopt import docopt
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import FileHistory

from kelvin.commands import CommandDispatcher
from kelvin.core import Chat, __version__


COMMANDS = ['/convo', '/switch', '/prompts', '/prompt', '/inject', '/model', '/show', '/help', '/quit']


def run_shell_command(cmd: str) -> None:
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if proc.stdout:
            print(proc.stdout.rstrip('\n'))
        if proc.returncode != 0:
            print(f"(exit {proc.returncode})")
    except OSError as exc:
        print(f"Shell error: {exc}")


def page_response(response_iter) -> None:
    """Page response, handling both streaming and non-streaming iterators."""
    pager = os.environ.get('PAGER', 'less -RX')
    pager_cmd = shlex.split(pager) if pager else []
    if not pager_cmd:
        # No pager: just print chunks
        for chunk in response_iter:
            print(chunk, end='', flush=True)
        print()
        return
    
    # Start pager process
    try:
        pager_proc = subprocess.Popen(
            pager_cmd,
            stdin=subprocess.PIPE,
            text=True,
        )
        # Feed chunks to pager
        for chunk in response_iter:
            pager_proc.stdin.write(chunk)
            pager_proc.stdin.flush()
        pager_proc.stdin.close()
        pager_proc.wait()
    except OSError:
        # Fall back to direct printing
        for chunk in response_iter:
            print(chunk, end='', flush=True)
        print()


class ChatCompleter(Completer):
    def __init__(self, core: Chat):
        self.core = core

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        words = text.split()
        if not text:
            for cmd in COMMANDS:
                yield Completion(cmd, start_position=0)
            return
        if len(words) == 1 and text.startswith('/'):
            for cmd in COMMANDS:
                if cmd.startswith(text):
                    yield Completion(cmd, start_position=-len(text))
            return
        if len(words) < 2 or not words[0].startswith('/'):
            return
        cmd = words[0]
        arg = words[-1]
        start_pos = -len(arg)
        try:
            if cmd == '/switch':
                base_path = Path.cwd()
                for item in base_path.glob(arg + '*'):
                    if item.is_dir():
                        yield Completion(str(item.relative_to(base_path)), start_position=start_pos)
            elif cmd == '/inject':
                if arg.startswith('./'):
                    base_path = self.core.context
                    for item in base_path.glob(arg[2:] + '*'):
                        yield Completion('./' + str(item.relative_to(base_path)), start_position=start_pos)
                elif arg.startswith('/'):
                    if '/' in arg:
                        base_path = Path(arg).parent
                        search_pattern = Path(arg).name + '*'
                        if base_path.exists():
                            for item in base_path.glob(search_pattern):
                                yield Completion(str(item), start_position=start_pos)
                else:
                    base_path = Path.cwd()
                    for item in base_path.glob(arg + '*'):
                        yield Completion(str(item.relative_to(base_path)), start_position=start_pos)
        except OSError:
            pass


def build_status_line(core: Chat) -> str:
    status = core.get_status_data()
    parts = [f"context: {status['context']}"]
    if status['convo'] and status['convo_title']:
        parts.append(f"convo: {status['convo_title']} ({status['convo'][:8]})")
    if status['prompts']:
        parts.append("prompts: " + ', '.join(prompt['name'] for prompt in status['prompts']))
    parts.append(f"model: {status['model']}")
    return ' | '.join(parts)


def run_repl(core: Chat) -> None:
    dispatcher = CommandDispatcher(core)
    history_file = core.kelvin_home.history_file()
    history = FileHistory(str(history_file))
    completer = ChatCompleter(core)
    session = PromptSession(history=history, completer=completer)
    print("Welcome to Lab Infra Chat CLI")
    print("Type /help for commands, /quit to exit")
    print()
    while True:
        try:
            print(f"\n{build_status_line(core)}")
            line = session.prompt(">>> ").strip()
            if not line:
                continue
            if line.startswith('!'):
                cmd = line[1:].strip()
                if cmd:
                    run_shell_command(cmd)
                continue
            if dispatcher.dispatch(line):
                continue
            response_iter = core.send_message(line)
            page_response(response_iter)
        except KeyboardInterrupt:
            print("\nUse /quit to exit")
        except EOFError:
            print("\nGoodbye!")
            break
        except Exception as exc:
            print(f"Error: {exc}")


def main() -> None:
    args = docopt(__doc__, version=f'Lab Infra Chat CLI {__version__}')
    run_repl(Chat(args.get('--config')))


if __name__ == '__main__':
    main()
