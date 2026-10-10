# Repository Guidelines

## Project Structure & Module Organization

`app/` contains the Python application: `console.py` serves the FastAPI console, `voice_agent.py` runs the Pipecat voice pipeline, and `chat_app.py` handles text chat. Context building, rendering, summarizing, and updates live in separate modules. `app/static/console.html` contains the UI; `app/agent_config.yaml` holds prompts and runtime settings.

`buyer.schema.md` and `seller.schema.md` define context sections. `samples/<glid>/<role>.md` contains reference contexts. Local `datasets/` supplies source records; generated `contexts/` stores current files and version history, while `memory/` stores transcripts and logs. No test directory currently exists.

## Build, Test, and Development Commands

- `./setup.sh`: create `app/.venv`, install dependencies, and initialize configuration; requires Python 3.11+.
- `./start.sh`: serve the console at `http://localhost:8000`; use `PORT=8080 ./start.sh` for another port. Voice uses port 7860.
- `source app/.venv/bin/activate`: activate the development environment.
- From `app/`, run `python view_builder.py 7444262 seller` to find or build context; append `--force` to rebuild from sources.
- `python -m compileall -q app`: check Python syntax from the repository root.

There is no separate build step. Keep dependency declarations in `app/requirements.txt` and `app/pyproject.toml` synchronized.

## Coding Style & Naming Conventions

Use four-space Python indentation, `snake_case` functions and modules, and uppercase constants. Follow existing type annotations and module docstrings. Keep context logic in focused modules and configurable prompts in YAML. Preserve schema headings and the `<glid>/<role>.md` naming pattern. No formatter or linter is configured; match surrounding formatting.

## Testing Guidelines

No automated framework or coverage threshold is configured. For changes, check syntax and smoke-test Prepare context, chat completion, context updates, and subsequent voice continuity. Verify cold-start behavior with GLID `900000000001` and inspect version history and Flow events. Use headphones for voice checks. Future automated tests should use `test_*.py` names and mock external services.

## Commit & Pull Request Guidelines

Git history is unavailable in this checkout. Use concise imperative commit subjects, such as `Fix context history preservation`. Describe changed behavior, validation performed, and configuration changes in pull requests. Link relevant issues and include screenshots for console changes.

## Security & Configuration Tips

Keep credentials in `app/.env`; never commit keys, datasets, generated contexts, or transcripts. Preserve API redaction and sample files. Forced rebuilds replace conversation updates in the current context; previous versions remain in history.
