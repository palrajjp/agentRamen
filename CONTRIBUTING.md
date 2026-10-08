# Contributing

Thanks for helping improve agentRamen. Small, focused changes are easiest to review.

## Set up

```bash
git clone https://github.com/palrajjp/agentRamen.git
cd agentRamen
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Install an optional extra when changing its integration, for example `python -m pip install -e '.[treesitter]'`.

## Verify changes

```bash
python -m unittest discover -s tests -v
python -m agentramen.cli status --json
```

For indexing or retrieval changes, add a focused regression test. Keep optional parser and embedding behavior isolated so the default installation remains dependency-free.

## Open a pull request

Describe the user problem, the behavior changed, and the checks run. Include representative CLI output for user-facing changes, and never include private repository data, credentials, or generated `.agentramen/graph.db` files.