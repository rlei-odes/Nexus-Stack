---
title: "Set up an isolated Python environment with uv in code-server"
description: "Create a clean Python venv using uv and wire it up as a Jupyter notebook kernel"
order: 2
---

# Set up an isolated Python environment with uv in code-server

Every Python project should have its own virtual environment — separate dependencies, no conflicts, reproducible. This tutorial uses **`uv`**, a fast Python package manager (written in Rust) that's preinstalled on the Nexus-Stack code-server. It's dramatically faster than `pip` and handles the venv for you.

## Prerequisites

- Nexus-Stack with `code-server` enabled
- Familiar with opening a terminal — see [Run curl in the code-server terminal](/docs/tutorials/code-server/terminal-curl/)

## Create a project folder

In a code-server terminal:

```bash
mkdir -p ~/my-project && cd ~/my-project
```

## Create the venv

```bash
uv venv .venv
```

`.venv` is the conventional name — it works with most IDEs and notebook integrations out of the box. You can name it anything, but don't.

Output looks like:
```
Using Python 3.12.x
Creating virtual environment at: .venv
Activate with: source .venv/bin/activate
```

## Activate it

```bash
source .venv/bin/activate
```

Your prompt gets a `(my-project)` or `(.venv)` prefix. From here, `python` and `pip` point to the binaries inside `.venv/bin/`, fully isolated from the system Python.

To deactivate later: `deactivate`.

## Install packages

With the venv active, two styles:

**Quick one-off install:**
```bash
uv pip install confluent-kafka pandas
```

**From a requirements file (recommended for anything beyond throwaway scripts):**

```bash
# Write the file
cat > requirements.txt <<'EOF'
confluent-kafka
pandas
jupyter
EOF

# Install everything at once
uv pip install -r requirements.txt
```

`uv pip install` is a drop-in replacement for `pip install` — same syntax, same package index, just faster.

## Use the venv in a Jupyter notebook

This is where the usual setup trips people up. After you create a `.ipynb` file and open it in code-server, the editor needs to be told **which Python kernel to run cells with**. It doesn't auto-pick the venv.

### Install ipykernel (once per venv)

```bash
uv pip install ipykernel
```

### Select the kernel

1. Open (or create) your `.ipynb` file in code-server
2. Top-right of the notebook: click **"Select Kernel"**
3. **"Python Environments..."**
4. Pick the one ending in `.venv` under your project folder (e.g. `my-project (Python 3.12.x) ~/my-project/.venv/bin/python`)

VS Code remembers this choice per notebook. You only need to do it once per notebook file.

### Verify

Add a cell:

```python
import sys
print(sys.executable)
```

Run it. Output should be `/home/coder/my-project/.venv/bin/python` (or similar) — confirming you're running inside the venv.

## Why `uv` over plain `python -m venv`

- **Speed** — `uv venv` takes <1 second; `python -m venv` takes 5–10 seconds (copies the interpreter).
- **Package installs are 10–100× faster** than pip because uv parallelizes downloads and uses a content-addressed cache.
- **Better resolver** — fewer surprises when packages have conflicting sub-dependencies.

You can still `python -m venv` if you prefer; both produce compatible `.venv/` directories that activate the same way.

## Common issues

**`ipykernel` not installed** → "Select Kernel" doesn't list your venv. Fix: `uv pip install ipykernel` with the venv active, then reload the notebook.

**Wrong kernel after a reload** → VS Code sometimes loses the per-notebook selection. Just pick it again.

**Running `python` gives a different Python** → you didn't `source .venv/bin/activate`. Check: `which python` should show a path inside `.venv/bin/`.

**Package installed but notebook can't import it** → almost always: you installed in one venv but the notebook is using a different kernel. Check: `!which python` in the notebook and compare to `which python` in the terminal.

## Next steps

- [Send your first event with a Python producer](/docs/tutorials/redpanda/first-producer/) — uses exactly this setup pattern
- [Read events with a Python consumer](/docs/tutorials/redpanda/python-consumer/) — the companion
