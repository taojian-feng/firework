#!/bin/bash
# Build the submission ZIP from committed files, plus the database so reviewers can skip setup.sh.
# Never includes .env or .venv. Output: dist/text-to-sql-submission.zip
set -e
cd "$(dirname "$0")/.."
mkdir -p dist
OUT=dist/text-to-sql-submission.zip
rm -f "$OUT"
git archive --format=zip --prefix=text-to-sql/ -o "$OUT" HEAD
if [ -f data/financial.sqlite ]; then
    STAGE=$(mktemp -d)
    trap 'rm -rf "$STAGE"' EXIT
    mkdir -p "$STAGE/text-to-sql/data"
    cp data/financial.sqlite "$STAGE/text-to-sql/data/"
    (cd "$STAGE" && zip -q -r "$OLDPWD/$OUT" text-to-sql)
fi
echo "Wrote $OUT ($(du -h "$OUT" | cut -f1))"
