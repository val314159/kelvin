import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml as pyyaml
from kelvin.storage.files import ensure_dir, make_read_only


YAML_PAT = '[0-9]*.yaml'


class ConvoStore:
    def __init__(self, convos_dir: Path):
        self.convos_dir = convos_dir
        ensure_dir(convos_dir)

    def get_convo_path(self, convo_id: str) -> Path:
        return self.convos_dir / convo_id

    def get_next_file_number(self, convo_dir: Path) -> str:
        existing = list(convo_dir.glob(YAML_PAT))
        if not existing:
            return f"{1:04d}"
        existing.sort()
        num_part = existing[-1].stem.split('-', 1)[0]
        next_val = int(num_part, 10) + 1
        return f"{next_val:04d}"

    def write_convo_file(self, convo_id: str, content: Any, file_type: str) -> Path:
        convo_dir = self.get_convo_path(convo_id)
        ensure_dir(convo_dir)
        filename = f"{self.get_next_file_number(convo_dir)}-{file_type}.yaml"
        filepath = convo_dir / filename
        with open(filepath, 'w') as f:
            pyyaml.dump(content, f, default_flow_style=False)
        make_read_only(filepath)
        return filepath

    def create_convo(self, context_convos_dir: Path, meta: Dict[str, Any], name: str) -> str:
        convo_id = str(uuid.uuid4())
        convo_dir = self.get_convo_path(convo_id)
        ensure_dir(convo_dir)
        meta = dict(meta)
        meta['uuid'] = convo_id
        self.write_convo_file(convo_id, meta, 'meta')
        self.create_convo_symlink(context_convos_dir, convo_id, name)
        return convo_id

    def create_convo_symlink(self, context_convos_dir: Path, convo_id: str, name: str) -> Path:
        ensure_dir(context_convos_dir)
        safe_name = name.lower().replace(' ', '-').replace('/', '-')
        symlink_path = context_convos_dir / f"{safe_name}.yaml"
        target_path = self.get_convo_path(convo_id)
        if symlink_path.exists():
            symlink_path.unlink()
        relative_target = os.path.relpath(target_path, context_convos_dir)
        symlink_path.symlink_to(relative_target)
        return symlink_path

    def list_context_convos(self, context_convos_dir: Path) -> List[tuple[str, Path]]:
        if not context_convos_dir.exists():
            return []
        convos: List[tuple[str, Path]] = []
        for symlink in context_convos_dir.glob(YAML_PAT):
            if symlink.is_symlink():
                convos.append((symlink.stem, symlink.readlink()))
        return convos

    def resolve_context_convo(self, context_convos_dir: Path, convo_name: str) -> Optional[str]:
        symlink_path = context_convos_dir / f"{convo_name}.yaml"
        if symlink_path.exists() and symlink_path.is_symlink():
            return symlink_path.readlink().name
        return None

    def load_convo_history(self, convo_id: Optional[str]) -> List[Dict[str, Any]]:
        if not convo_id:
            return []
        convo_dir = self.get_convo_path(convo_id)
        history: List[Dict[str, Any]] = []
        for filepath in sorted(convo_dir.glob(YAML_PAT)):
            if filepath.name.endswith('-meta.yaml'):
                continue
            with open(filepath, 'r') as f:
                content = pyyaml.safe_load(f)
            if content:
                history.extend(content)
        return history

    def load_meta(self, convo_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not convo_id:
            return None
        meta_file = self.get_convo_path(convo_id) / '0001-meta.yaml'
        if not meta_file.exists():
            return None
        with open(meta_file, 'r') as f:
            meta = pyyaml.safe_load(f)
        return meta or None
