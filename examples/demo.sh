#!/usr/bin/env bash
# 30-second GitGraph demo: builds a tiny repo, indexes it, and queries it.
set -euo pipefail
dir=$(mktemp -d)
cd "$dir"
git init -q
git config user.email demo@example.com
git config user.name demo
mkdir src
printf 'def login(user):\n    return True\n' > src/auth.py
printf 'from auth import login\n\ndef main():\n    login("a")\n' > src/app.py
git add . && git commit -qm "Add auth and app"
gitgraph init
gitgraph context "Add authentication" --budget 2000
gitgraph impact src/auth.py
echo "Demo repo: $dir"
