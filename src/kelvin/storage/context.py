import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from kelvin.storage.files import ensure_dir, load_json_file, save_json_file


class ContextStore:
    def __init__(self, context: Path, auto_inject_makefile: bool):
        self.context = context
        self.auto_inject_makefile = auto_inject_makefile

    def set_context(self, context: Path) -> None:
        self.context = context

    def get_context_convos_dir(self) -> Path:
        return self.context / '.kelvin'

    def get_context_state_file(self) -> Path:
        return self.get_context_convos_dir() / 'context_state.json'

    def get_local_injected_file(self) -> Path:
        return self.get_context_convos_dir() / 'local.injected.txt'

    def load_state(self) -> Dict[str, Any]:
        return load_json_file(self.get_context_state_file(), {})

    def save_state(self, state: Dict[str, Any]) -> bool:
        return save_json_file(self.get_context_state_file(), state)

    def load_injected_file(
        self,
        file_path: Path,
        *,
        base_context: Optional[Path] = None,
        check_duplicates: bool = False,
        existing_files: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        if not file_path.exists():
            return []
        if base_context is None:
            base_context = self.context
        injected_files: List[Dict[str, Any]] = []
        seen = set(existing_files or ())
        try:
            with open(file_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if line.startswith('./'):
                        stored_path = str(base_context / line[2:])
                    elif line.startswith('/'):
                        abs_path = Path(line)
                        if not abs_path.exists():
                            print(f"Warning: Absolute path {line} does not exist, skipping")
                            continue
                        stored_path = str(abs_path)
                    else:
                        full_path = Path.cwd() / line
                        if not full_path.exists():
                            print(f"Warning: Path {line} does not exist, skipping")
                            continue
                        stored_path = str(full_path)
                    if Path(stored_path).exists():
                        if not check_duplicates or stored_path not in seen:
                            injected_files.append({
                                'file': stored_path,
                                'injected_at': datetime.datetime.now().isoformat() + 'Z',
                            })
                            seen.add(stored_path)
        except OSError:
            print(f"Warning: Failed to load injected file from {file_path}")
        return injected_files

    def get_injected_files(self) -> List[Dict[str, Any]]:
        injected_files: List[Dict[str, Any]] = []
        if self.auto_inject_makefile:
            makefile_path = self.context / 'Makefile'
            if makefile_path.exists():
                injected_files.append({
                    'file': str(makefile_path),
                    'injected_at': datetime.datetime.now().isoformat() + 'Z',
                })
        injected_txt_path = self.context / 'injected.txt'
        injected_files.extend(self.load_injected_file(injected_txt_path))
        manual_injections = self.load_injected_file(self.get_local_injected_file(), check_duplicates=False)
        injected_files.extend(manual_injections)
        return injected_files

    def save_injected_file(
        self,
        file_path: Path,
        injections: List[str],
        *,
        header: Optional[str] = None,
        mode: str = 'write',
    ) -> bool:
        try:
            ensure_dir(file_path.parent)
            if mode == 'append':
                with open(file_path, 'a') as f:
                    if header and not file_path.exists():
                        f.write(f"{header}\n")
                    for injection in injections:
                        f.write(f"{injection}\n")
            else:
                with open(file_path, 'w') as f:
                    if header:
                        f.write(f"{header}\n")
                    for injection in injections:
                        f.write(f"{injection}\n")
            return True
        except OSError:
            print(f"Warning: Failed to save injected file to {file_path}")
            return False

    def drop_injected_file(self, target: str) -> bool:
        file_path = self.get_local_injected_file()
        try:
            raw_lines: List[str] = []
            if file_path.exists():
                with open(file_path, 'r') as f:
                    raw_lines = f.readlines()
            filtered_lines = []
            for line in raw_lines:
                stripped = line.strip()
                if stripped != target and not stripped.startswith('# ' + target):
                    filtered_lines.append(line)
            ensure_dir(file_path.parent)
            with open(file_path, 'w') as f:
                f.writelines(filtered_lines)
            return True
        except OSError:
            print(f"Warning: Failed to drop {target} from {file_path}")
            return False

    def list_context_dirs(self, root: Path, current_context: Path) -> List[tuple[str, bool]]:
        contexts: List[tuple[str, bool]] = []
        for item in root.iterdir():
            if item.is_dir() and not item.name.startswith('.'):
                contexts.append((item.name, item == current_context))
        return contexts

    def resolve_switch_target(self, path: str) -> Path:
        return (Path.cwd() / path).resolve()
