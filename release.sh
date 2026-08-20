#!/usr/bin/env bash
# release.sh — cut a new release: bump version, tag, push (CI publishes to npm).
#
#   Usage:  ./release.sh [major|minor|patch|1.2.3]
#   Example: ./release.sh patch     →  0.1.2   (default)
#            ./release.sh 0.2.0     →  0.2.0
#
# Requires a clean working tree. After the push, GitHub Actions publishes
# the package to npm automatically (see .github/workflows/publish.yml).
set -euo pipefail
cd "$(dirname "$0")"

BUMP="${1:-patch}"

# Validate the working tree is clean (avoid shipping uncommitted changes)
if [ -n "$(git status --porcelain)" ]; then
  echo "error: working tree is not clean. Commit or stash changes first." >&2
  exit 1
fi

echo "Bumping version ($BUMP) ..."
# npm version bumps package.json, commits, and creates a git tag.
if [[ "$BUMP" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  npm version "$BUMP" -m "chore: release v%s"
else
  npm version "$BUMP" -m "chore: release v%s"
fi

VERSION="$(node -p "require('./package.json').version")"
echo "Pushing tag v$VERSION (CI will publish to npm) ..."
git push --follow-tags origin main

echo "Done. Release v$VERSION is on its way — watch the Actions tab:"
echo "  https://github.com/caius-kong/ccusage-dashboard/actions"