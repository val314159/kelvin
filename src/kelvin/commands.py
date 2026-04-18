import sys
from pathlib import Path

from kelvin.help import HELP_MESSAGE


class CommandDispatcher:
    def __init__(self, core):
        self.core = core

    def dispatch(self, line: str) -> bool:
        if not line.startswith('/'):
            return False
        parts = line.split()
        command = parts[0]
        args = parts[1:] if len(parts) > 1 else []
        if command == '/help':
            print(HELP_MESSAGE)
        elif command in ('/quit', '/exit'):
            print("Goodbye!")
            sys.exit(0)
        elif command == '/show':
            self.handle_show(args)
        elif command == '/status':
            self.show_status()
        elif command == '/history':
            self.show_history()
        elif command == '/convo':
            self.handle_convo(args)
        elif command == '/switch':
            self.handle_switch(args)
        elif command == '/prompts':
            self.handle_prompts()
        elif command == '/prompt':
            self.handle_prompt(args)
        elif command == '/inject':
            self.handle_inject(args)
        elif command == '/model':
            self.handle_model(args)
        else:
            print(f"Unknown command: {command}")
        return True

    def handle_show(self, args):
        if not args:
            print("Usage: /show <subcommand>")
            print("  config   - Show current configuration")
            print("  status   - Show current status")
            print("  history  - Show conversation history")
            return
        if args[0] == 'config':
            self.show_config()
        elif args[0] == 'status':
            self.show_status()
        elif args[0] == 'history':
            self.show_history()
        else:
            print(f"Unknown show subcommand: {args[0]}")
            print("Available: config, status, history")

    def handle_convo(self, args):
        if not args or args[0] == 'list':
            convos = self.core.convo_store.list_context_convos(self.core.context_store.get_context_convos_dir())
            if convos:
                print("Conversations in this context:")
                for convo_name, target in convos:
                    print(f"  {convo_name} -> {target}")
            else:
                print("No conversations in this context")
        elif args[0] == 'new':
            convo_id = self.core.create_convo(args[1] if len(args) > 1 else None)
            print(f"Created conversation: {convo_id}")
        elif args[0] == 'fork':
            print("Forking not yet implemented")
        elif self.core.switch_convo(args[0]):
            print(f"Switched to conversation: {args[0]}")
        else:
            print(f"Conversation '{args[0]}' not found")

    def handle_switch(self, args):
        if not args or args[0] == 'list':
            print("Available contexts:")
            for name, is_current in self.core.context_store.list_context_dirs(Path.cwd(), self.core.context):
                marker = " (current)" if is_current else ""
                print(f"  {name}{marker}")
        else:
            self.core.switch_context(args[0])

    def handle_prompts(self):
        print("Available prompts:")
        for group_name, prompt_names in self.core.prompt_store.list_prompts().items():
            print(f"\n{group_name}:")
            for prompt_name in prompt_names:
                print(f"  {prompt_name}")

    def handle_prompt(self, args):
        if not args:
            print("Current prompts:")
            for prompt in self.core.prompts:
                print(f"  {prompt['name']} v{prompt['version']}")
            return
        if args[0] == 'add':
            if len(args) < 2:
                print("Usage: /prompt add <prompt_name>")
                return
            try:
                self.core.add_prompt(args[1])
                print(f"Added prompt: {args[1]}")
            except ValueError as exc:
                print(f"Error: {exc}")
        elif args[0] == 'drop':
            if len(args) < 2:
                print("Usage: /prompt drop <prompt_name>")
                return
            self.core.drop_prompt(args[1])
            print(f"Dropped prompt: {args[1]}")
        else:
            print("Unknown prompt command. Use: add, drop")

    def handle_inject(self, args):
        if not args or args[0] == 'list':
            print("Currently injected files:")
            for injected in self.core.context_store.get_injected_files():
                print(f"  {injected['file']}")
            return
        if args[0] == 'clear':
            if self.core.clear_injected_files():
                print("Cleared injected files")
            else:
                print(f"Warning: Failed to clear {self.core.context_store.get_local_injected_file()}")
        elif args[0] == 'drop':
            if len(args) < 2:
                print("Usage: /inject drop <file>")
                return
            if self.core.drop_injected_file(args[1]):
                print(f"Dropped injected file: {args[1]}")
            else:
                print(f"Warning: Failed to drop {args[1]} from {self.core.context_store.get_local_injected_file()}")
        else:
            stored_path = self.core.add_injected_file(args[0])
            if stored_path is None:
                print(f"File '{args[0]}' not found")
            elif stored_path == '':
                print(f"Warning: Failed to add {args[0]} to {self.core.context_store.get_local_injected_file()}")
            else:
                print(f"Injected file: {stored_path}")

    def handle_model(self, args):
        if not args:
            print(f"Current model: {self.core.model} ({self.core.endpoint})")
            return
        if args[0] == 'list':
            print("Available endpoints:")
            for endpoint in self.core.endpoints.keys():
                print(f"  {endpoint}")
            return
        model_name = args[0]
        endpoint = None
        if ':' in model_name:
            endpoint, model_name = model_name.split(':', 1)
        try:
            if endpoint is not None:
                self.core.set_endpoint(endpoint)
            self.core.set_model(model_name)
            print(f"Switched to model: {self.core.model} ({self.core.endpoint})")
        except ValueError as exc:
            print(f"Error: {exc}")

    def show_status(self):
        status = self.core.get_status_data()
        print(f"context: {status['context']}")
        print(f"convo:   {status['convo'] or 'None'}")
        if status['prompts']:
            prompts_str = ', '.join(f"{prompt['name']} v{prompt['version']}" for prompt in status['prompts'])
            print(f"prompts: {prompts_str}")
        print(f"model:   {status['model']} ({status['endpoint']})")

    def show_config(self):
        config = self.core.get_config_data()
        print("Current Configuration:")
        print(f"  Model: {config['model']} ({config['endpoint']})")
        print(f"  Context: {config['context']}")
        print(f"  Conversation Store: {config['convos_dir']}")
        print(f"  Prompt Library: {config['prompts_dir']}")
        print(f"  Auto-inject Makefile: {config['auto_inject_makefile']}")
        print(f"  History File: {config['history_file']}")
        print(f"  Current Conversation: {config['convo'] or 'None'}")
        if config['prompts']:
            print(f"  Active Prompts: {len(config['prompts'])}")
            for prompt in config['prompts']:
                print(f"    - {prompt['name']} v{prompt['version']}")
        else:
            print("  Active Prompts: None")
        if config['injected_files']:
            print(f"  Injected Files: {len(config['injected_files'])}")
            for injected in config['injected_files']:
                print(f"    - {injected['file']}")
        else:
            print("  Injected Files: None")

    def show_history(self):
        if not self.core.convo:
            print("No active conversation")
            return
        for msg in self.core.convo_store.load_convo_history(self.core.convo):
            if msg['role'] == 'user':
                print(f"User: {msg['content']}")
            elif msg['role'] == 'assistant':
                print(f"Asst: {msg['content']}")
            elif msg['role'] == 'tool':
                print(f"Tool: {msg['content']}")
            print()
