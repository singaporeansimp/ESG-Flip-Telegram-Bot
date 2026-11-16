#!/usr/bin/env bash
#
# Back up the ESG Flip bot's SQLite database, and prune the old copies.
#
# This is a thin wrapper around "python -m esgbot.backup", which does the real
# work with SQLite's online backup API: a consistent copy of a WAL database the
# bot is still writing to, checked with PRAGMA integrity_check before it is given
# its final name. Nothing beyond the bot's own virtual environment is needed --
# in particular not the sqlite3 command-line tool, which a small VPS often does
# not have.
#
# Where things go, all optional:
#
#   ESGBOT_DB_PATH            the live database      (default esgbot.sqlite3)
#   ESGBOT_BACKUP_DIR         where copies go        (default ./backups)
#   ESGBOT_BACKUP_KEEP_DAYS   days to keep, 0 = all  (default 14)
#   ESGBOT_PYTHON             the interpreter        (default ../.venv/bin/python)
#
# Any arguments are passed through, so a one-off copy somewhere else is
#
#   deploy/backup.sh --backup-dir /tmp/esg-check
#
# The bot's token is not needed here and is deliberately not read: the only
# secrets this script can reach are the ones inside the database itself, which
# are the revealed seeds of finished flips.

set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python="${ESGBOT_PYTHON:-${root}/.venv/bin/python}"

if [[ ! -x "${python}" ]]; then
	printf 'esgbot backup: no usable Python at %s\n' "${python}" >&2
	printf 'Run "uv sync --no-dev" in %s, or point ESGBOT_PYTHON at a Python that can import esgbot.\n' \
		"${root}" >&2
	exit 1
fi

# PYTHONPATH rather than cd, so a relative ESGBOT_DB_PATH still means what it
# meant to whoever ran this.
exec env PYTHONPATH="${root}${PYTHONPATH:+:${PYTHONPATH}}" "${python}" -m esgbot.backup "$@"
