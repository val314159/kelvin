import os
import datetime
from functools import partial
from pathlib import Path
from typing import Dict, List, Optional, Any
from . import __version__
from .oai import OAI
from .sandbox import bounce_sandbox
from .storage import ConvoStore, ContextStore, KelvinHome, PromptStore

class Chat:
    def __init__(self, config_path: str):
        self.first_convo: Optional[str] = None
        self.prompts: List[str] = []
        self.context = Path.cwd()
        self.kelvin_home = KelvinHome()
        self.config = self.kelvin_home.load_config(config_path)

        self.model = self.config['default_model']
        self.endpoint = self.config['default_endpoint']
        self.endpoints = self.config['endpoints']
        self.stream = bool(self.config.get('stream', True))
        self.restore_last_convo = bool(self.config.get('restore_last_convo', True))
        self.auto_inject_makefile = bool(self.config.get('auto_inject_makefile', True))

        self.context_store = ContextStore(
            self.context,
            self.auto_inject_makefile,
        )
        self.convo_store = ConvoStore(self.kelvin_home.convos_dir())
        self.prompt_store = PromptStore(self.kelvin_home.ensure_prompt_dir())

        # Get max_tool_iterations with validation
        raw_value = self.config.get('max_tool_iterations', 15)
        try:
            max_tool_iterations = int(str(raw_value), 10)
        except (ValueError, TypeError):
            print(f"Warning: Invalid max_tool_iterations ({raw_value}), using default 15")
            max_tool_iterations = 15

        # Enforce reasonable bounds
        if max_tool_iterations < 1:
            print(f"Warning: max_tool_iterations ({max_tool_iterations}) too low, using 15")
            max_tool_iterations = 15
        elif max_tool_iterations > 100:
            print(f"Warning: max_tool_iterations ({max_tool_iterations}) too high, clamping to 100")
            max_tool_iterations = 100

        self.oai = OAI(self.endpoints, max_tool_iterations)
        
        self.setup_openai()
        self.load_user_state()
        self.load_context_state()

    def setup_openai(self):
        self.oai.use_endpoint(self.endpoint, self.model, self.stream)
    
    def load_user_state(self):
        state = self.kelvin_home.load_user_state()
        if state is None:
            return
        ctx = state.get('last_context')
        if ctx:
            candidate = Path(ctx)
            if not candidate.is_absolute():
                candidate = Path.cwd() / ctx
            if candidate.exists() and candidate.is_dir():
                self.set_context(candidate)  # Use centralized method to chdir
        fc = state.get('first_convo')
        if fc:
            self.first_convo = fc

    def set_context(self, new_context: Path):
        """Set current context and change working directory."""
        self.context = new_context
        self.context_store.set_context(new_context)
        os.chdir(new_context)  # Single place for chdir
        # Save pointer to new context
        self.save_user_state()
        bounce_sandbox(str(new_context))

    def save_user_state(self):
        state = {
            'last_context': str(self.context.resolve()),
            'first_convo': self.first_convo,
            'saved_at': datetime.datetime.now().isoformat() + 'Z',
        }
        self.kelvin_home.save_user_state(state)

    def load_context_state(self):
        state = self.context_store.load_state()
        self.context_state = state
        # Find last conversation for this context
        last_convo = state.get('last_convo')
        if last_convo:
            candidates = [last_convo]
        else:
            candidates = []
        # Try to find a valid conversation
        selected = None
        for candidate in candidates:
            convo_dir = self.convo_store.get_convo_path(candidate)
            if convo_dir.exists() and convo_dir.is_dir():
                selected = candidate
                break
        self.convo = selected

    def set_context_convo(self, convo_id: Optional[str]):
        self.convo = convo_id
        if not self.context_state:
            self.context_state = {}
        self.context_state['last_convo'] = convo_id
        self.context_store.save_state(self.context_state)

    def build_meta_state(self, *, include_title: bool = False,
                         title: Optional[str] = None,
                         convo_id: Optional[str] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            'timestamp': datetime.datetime.now().isoformat() + 'Z',
            'context': str(self.context),
            'model': self.model,
            'endpoint': self.endpoint,
            'prompts': [
                {
                    'prompt': prompt['name'],
                    'version': prompt['version'],
                    'snapshot': prompt['snapshot']
                } for prompt in self.prompts
            ]
        }
        if convo_id is not None:
            payload['uuid'] = convo_id
        if include_title:
            if title is not None:
                payload['title'] = title
        return payload

    def build_context(self) -> str:
        """Build the full context for the AI."""
        context_parts = []
        # Add prompts
        for prompt in self.prompts:
            context_parts.append(f"=== PROMPT: {prompt['name']} v{prompt['version']} ===")
            context_parts.append(prompt['snapshot'])
            context_parts.append("")
        # Add injected files
        context_parts.extend(
            self.prompt_store.read_injected_content(self.context_store.get_injected_files())
        )
        # Add context info
        context_parts.append(f"=== CURRENT CONTEXT ===")
        context_parts.append(f"Directory: {self.context}")
        context_parts.append(f"Conversation: {self.convo or 'None'}")
        context_parts.append(f"Model: {self.model} ({self.endpoint})")
        context_parts.append("")
        return "\n".join(context_parts)

    def write_convo_file(self, msg, role):
        return self.convo_store.write_convo_file(self.convo, msg, role)
        
    def write_meta_update(self):
        if not self.convo:
            return
        meta = self.build_meta_state(convo_id=self.convo)
        self.write_convo_file(meta, 'meta')

    def create_convo(self, name: str = None) -> str:
        """Create a new conversation."""
        if name is None:
            name = f"convo-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
        meta = self.build_meta_state(include_title=True, title=name)
        meta['fork_of'] = None
        meta['fork_at'] = None
        meta['tags'] = []
        meta['status'] = 'active'
        convo_id = self.convo_store.create_convo(self.context_store.get_context_convos_dir(), meta, name)
        self.set_context_convo(convo_id)
        if not self.first_convo:
            self.first_convo = convo_id
        self.save_user_state()
        return convo_id

    def switch_convo(self, convo_name: str) -> bool:
        convo_id = self.convo_store.resolve_context_convo(self.context_store.get_context_convos_dir(), convo_name)
        if not convo_id:
            return False
        self.set_context_convo(convo_id)
        self.save_user_state()
        return True

    def add_prompt(self, prompt_name: str) -> Dict[str, Any]:
        prompt = self.prompt_store.load_prompt(prompt_name)
        self.prompts.append(prompt)
        self.write_meta_update()
        return prompt

    def drop_prompt(self, prompt_name: str) -> bool:
        before = len(self.prompts)
        self.prompts = [p for p in self.prompts if p['name'] != prompt_name]
        changed = len(self.prompts) != before
        if changed:
            self.write_meta_update()
        return changed

    def clear_injected_files(self) -> bool:
        header = "# Local injected files for this context\n# Add files to inject, comment out with # to disable\n"
        ok = self.context_store.save_injected_file(
            self.context_store.get_local_injected_file(),
            [],
            header=header,
            mode='write',
        )
        if ok:
            self.write_meta_update()
        return ok

    def drop_injected_file(self, file_path: str) -> bool:
        ok = self.context_store.drop_injected_file(file_path)
        if ok:
            self.write_meta_update()
        return ok

    def add_injected_file(self, file_path: str) -> Optional[str]:
        if file_path.startswith('./'):
            full_path = self.context / file_path[2:]
            stored_path = str(full_path)
        else:
            full_path = Path.cwd() / file_path
            stored_path = str(full_path)
        if not full_path.exists():
            return None
        ok = self.context_store.save_injected_file(
            self.context_store.get_local_injected_file(),
            [file_path],
            mode='append',
        )
        if not ok:
            return ''
        self.write_meta_update()
        return stored_path

    def send_message(self, message: str):
        """Send a message to the AI and return a generator for the response."""
        if not self.convo:
            self.create_convo()
        user_msg = [{
            'role': 'user',
            'content': message,
            'timestamp': datetime.datetime.now().isoformat() + 'Z'
        }]
        self.write_convo_file(user_msg, 'user')
        # Build full context
        full_context = self.build_context()
        # Add conversation history
        history = self.convo_store.load_convo_history(self.convo)
        messages = []
        # Add system context
        messages.append({'role': 'system', 'content': full_context})
        # Add history
        for msg in history:
            if msg['role'] in ['user', 'assistant', 'tool']:
                rec = {'role': msg['role'], 'content': msg.get('content')}
                if msg['role'] == 'assistant' and 'tool_calls' in msg:
                    rec['tool_calls'] = msg['tool_calls']
                if msg['role'] == 'tool':
                    rec['name'] = msg['name']
                    rec['tool_call_id'] = msg['tool_call_id']
                messages.append(rec)        
        try:
            for event in self.oai.process_turn(messages):
                if event['role'] == 'tool':
                    tool_msg = [{
                        'role': 'tool',
                        'tool_call_id': event['tool_call_id'],
                        'name': event['name'],
                        'content': event['content'],
                        'timestamp': event['timestamp'],
                    }]
                    self.write_convo_file(tool_msg, 'tool')
                elif event['done'] and event['role'] == 'assistant':
                    asst_msg = [{
                        'role': 'assistant',
                        'content': event['content'],
                        'timestamp': datetime.datetime.now().isoformat() + 'Z'
                    }]
                    if event['tool_calls']:
                        asst_msg[0]['tool_calls'] = event['tool_calls']
                        pass
                    self.write_convo_file(asst_msg, 'asst')
                    pass
                yield event
        except Exception as exc:
            yield {
                'round': 1,
                'seq': 1,
                'role': 'error',
                'content': str(exc),
                'tool_calls': [],
                'done': True,
            }
        
    
    def switch_context(self, path):
        """Perform context switching."""
        # Switch to specific context
        old_convo = self.convo
        new_context = (Path.cwd() / path).resolve()
        print(f"Switching to {new_context}...")
        if new_context.exists() and new_context.is_dir():
            self.set_context(new_context)
            self.load_context_state()  # Load context after changing directory
            new_convo = self.convo
            new_context_rel = str(self.context)
            if old_convo:
                leave_meta: Dict[str, Any] = {
                    'timestamp': datetime.datetime.now().isoformat() + 'Z',
                    'event': 'switch_context',
                    'to_context': new_context_rel,
                    'to_convo': new_convo,
                }
                self.write_convo_file(leave_meta, 'meta')
            print(f"Switched to context: {new_context}")
        else:
            print(f"Context '{path}' not found")

    def set_endpoint(self, endpoint: str) -> None:
        if endpoint not in self.endpoints:
            raise ValueError(f"Unknown endpoint: {endpoint}")
        self.endpoint = endpoint
        self.setup_openai()
        self.write_meta_update()

    def set_model(self, model_name: str) -> None:
        self.model = model_name
        self.setup_openai()
        self.write_meta_update()

    def get_status_data(self) -> Dict[str, Any]:
        meta = self.convo_store.load_meta(self.convo) if self.convo else None
        return {
            'context': self.context,
            'convo': self.convo,
            'convo_title': meta.get('title') if meta else None,
            'prompts': list(self.prompts),
            'model': self.model,
            'endpoint': self.endpoint,
        }

    def get_config_data(self) -> Dict[str, Any]:
        return {
            'model': self.model,
            'endpoint': self.endpoint,
            'context': self.context,
            'convos_dir': self.kelvin_home.convos_dir(),
            'prompts_dir': self.kelvin_home.ensure_prompt_dir(),
            'auto_inject_makefile': self.auto_inject_makefile,
            'history_file': self.kelvin_home.history_file(),
            'convo': self.convo,
            'prompts': list(self.prompts),
            'injected_files': self.context_store.get_injected_files(),
        }
