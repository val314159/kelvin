
UV_CACHE_DIR := .uv-cache
UV := UV_CACHE_DIR=$(UV_CACHE_DIR) uv

all: .venv
	$(UV) run -m codez

serve: .venv
	$(UV) run -m codez --host 0.0.0.0

.venv: pyproject.toml
	$(UV) sync

clean:
	find . -name \*~ -o -name .\*~ | xargs rm -fr

realclean: clean
	rm -fr uv.lock .venv .uv-cache __pycache__ *.log
	tree -a -I .kelvin .
