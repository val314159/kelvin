from collections.abc import Mapping
from typing import Any, Dict, Optional
from pathlib import Path

import yaml as pyyaml
from kelvin.storage.files import ensure_dir, load_json_file, save_json_file


class KelvinHome:
    def __init__(self):
        self.root = Path.home() / '.kelvin.d'

    def home_dir(self) -> Path:
        return self.root

    def convos_dir(self) -> Path:
        path = self.root / 'convos'
        ensure_dir(path)
        return path

    def prompts_dir(self) -> Path:
        return self.root / 'prompts'

    def ensure_prompt_dir(self) -> Path:
        """Ensure ~/.kelvin.d/prompts exists and copy package prompts if needed."""
        prompt_dir = self.prompts_dir()
        if prompt_dir.exists():
            return prompt_dir
        ensure_dir(prompt_dir)
        
        # Find package prompts directory (look relative to this script)
        # __file__ is in storage/home.py, so go up 3 levels to project root
        project_root = Path(__file__).parents[3]
        package_prompts = project_root / 'prompts'
        
        if package_prompts.exists() and package_prompts.is_dir():
            # Copy prompts if user dir is empty
            user_has_prompts = any(prompt_dir.glob('*'))
            if not user_has_prompts:
                import shutil
                for subdir in ['system', 'templates', 'workflows']:
                    src = package_prompts / subdir
                    dst = prompt_dir / subdir
                    if src.exists():
                        shutil.copytree(src, dst, dirs_exist_ok=True)
        
        return prompt_dir

    def state_file(self) -> Path:
        return self.root / 'chat_state.json'

    def config_file(self) -> Path:
        return self.root / 'config.yaml'

    def history_file(self) -> Path:
        path = self.root / 'cli-history'
        ensure_dir(path.parent)
        return path

    def load_user_state(self) -> Optional[Dict[str, Any]]:
        return load_json_file(self.state_file(), None)

    def save_user_state(self, state: Dict[str, Any]) -> bool:
        return save_json_file(self.state_file(), state)

    def load_config(self, config_path: str) -> Dict[str, Any]:
        default_config: Dict[str, Any] = {
            'default_model': 'firmen102/qwen3.5-27b',
            'default_endpoint': 'ollama',
            'endpoints': {
                'openai': {
                    'url': 'https://api.openai.com/v1',
                    'key_env': 'OPENAI_API_KEY',
                },
                'ollama': {
                    'url': 'http://localhost:11434/v1',
                    'key_env': 'dummy',
                },
            },
            'stream': True,
            'auto_inject_makefile': True,
            'restore_last_convo': True,
        }
        user_config_path = self.config_file()
        if user_config_path.exists():
            try:
                with open(user_config_path, 'r') as f:
                    loaded_config = pyyaml.safe_load(f)
                if not isinstance(loaded_config, Mapping):
                    raise ValueError(f"Config at {user_config_path} must be a mapping")
                default_config.update(loaded_config)
            except OSError:
                print(f"Warning: Failed to load config from {user_config_path}")
        else:
            try:
                ensure_dir(self.home_dir())
                with open(user_config_path, 'w') as f:
                    pyyaml.dump(default_config, f, indent=2)
                print(f"Created default config at: {user_config_path}")
            except OSError:
                print(f"Warning: Failed to create default config at {user_config_path}")
        if config_path:
            config_override = Path(config_path)
            if config_override.exists():
                try:
                    with open(config_override, 'r') as f:
                        loaded_config = pyyaml.safe_load(f)
                    if not isinstance(loaded_config, Mapping):
                        raise ValueError(f"Config at {config_override} must be a mapping")
                    default_config.update(loaded_config)
                except OSError:
                    print(f"Warning: Failed to load config from {config_override}")
        return default_config
