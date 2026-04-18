from pathlib import Path
from typing import Any, Dict, List

import yaml as pyyaml


class PromptStore:
    def __init__(self, prompts_dir: Path):
        self.prompts_dir = prompts_dir

    def load_prompt(self, prompt_name: str) -> Dict[str, Any]:
        prompt_path = None
        for subdir in ['system', 'templates', 'workflows']:
            candidate = self.prompts_dir / subdir / f"{prompt_name}.md"
            if candidate.exists():
                prompt_path = candidate
                break
        if not prompt_path:
            raise ValueError(f"Prompt '{prompt_name}' not found")
        with open(prompt_path, 'r') as f:
            content = f.read()
        if content.startswith('---'):
            parts = content.split('---', 2)
            if len(parts) >= 3:
                frontmatter = pyyaml.safe_load(parts[1])
                body = parts[2].strip()
                return {
                    'name': frontmatter['title'],
                    'version': frontmatter['version'],
                    'snapshot': body,
                    'frontmatter': frontmatter,
                }
        raise ValueError(f"Invalid prompt format in {prompt_name}")

    def list_prompts(self) -> Dict[str, List[str]]:
        prompts: Dict[str, List[str]] = {}
        for subdir in ['system', 'templates', 'workflows']:
            subdir_path = self.prompts_dir / subdir
            if subdir_path.exists():
                prompts[subdir.title()] = [prompt_file.stem for prompt_file in subdir_path.glob('*.md')]
        return prompts

    def read_injected_content(self, injected_files: List[Dict[str, Any]]) -> List[str]:
        context_parts: List[str] = []
        for injected in injected_files:
            context_parts.append(
                f"<injected file=\"{injected['file']}\" injected_at=\"{injected['injected_at']}\">"
            )
            full_path = Path(injected['file'])
            try:
                with open(full_path, 'r') as f:
                    context_parts.append(f.read())
            except OSError as exc:
                context_parts.append(f"Error reading injected file: {exc}")
            context_parts.append("</injected>")
            context_parts.append("")
        return context_parts
