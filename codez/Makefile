
all: .venv
	uv run -m codez

serve: .venv
	uv run -m codez --host 0.0.0.0

.venv: pyproject.toml
	uv sync

clean:
	find . -name \*~ -o -name .\*~ | xargs rm -fr

realclean: clean
	rm -fr uv.lock .venv __pycache__ *.log
	tree -a -I .kelvin .
