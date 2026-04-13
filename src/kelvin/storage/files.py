import json
from pathlib import Path
from typing import Any


PERM_IMMUTABLE = 0o444


def make_read_only(filepath):
    os.chmod(filepath, PERM_IMMUTABLE)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def load_json_file(file_path: Path, default: Any) -> Any:
    try:
        with open(file_path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError):
        print(f"Warning: Failed to load {file_path}")
        return default


def save_json_file(file_path: Path, data: Any) -> bool:
    try:
        ensure_dir(file_path.parent)
        with open(file_path, 'w') as f:
            json.dump(data, f, indent=2, sort_keys=True)
        return True
    except OSError:
        print(f"Warning: Failed to save {file_path}")
        return False
