"""The files in ``deploy/``: do they say what the README says they say.

None of this needs systemd to be running, and none of it is a substitute for
installing the units on a real VPS. What it does catch is the kind of mistake
that is invisible until the bot is live: a directive systemd would silently
ignore, a database path that drifted apart between the bot's environment file and
the backup unit's, a token handed to a job that has no business with it, or a
backup script that lost its executable bit on the way into the repository.

``systemd-analyze verify`` does the real parse where it is available, against
copies whose ``ExecStart`` has been pointed at a binary that exists here --
``/opt/esgbot`` does not exist on a development machine, and verify treats a
missing command as an error. Everything else about the units is left exactly as
it is, which is the part worth verifying.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

from esgbot.backup import BACKUP_DIR_ENV_VAR, KEEP_DAYS_ENV_VAR
from esgbot.logging_setup import LEVEL_ENV_VAR
from esgbot.telegram.settings import DB_PATH_ENV_VAR, TOKEN_ENV_VAR

DEPLOY: Final = Path(__file__).resolve().parent.parent / "deploy"

BOT_UNIT: Final = DEPLOY / "esgbot.service"
BACKUP_UNIT: Final = DEPLOY / "esgbot-backup.service"
BACKUP_TIMER: Final = DEPLOY / "esgbot-backup.timer"
ENV_EXAMPLE: Final = DEPLOY / "esgbot.env.example"
BACKUP_SCRIPT: Final = DEPLOY / "backup.sh"

STATE_DIRECTORY: Final = "/var/lib/esgbot"

HARDENING: Final = {
    "NoNewPrivileges": "true",
    "PrivateTmp": "true",
    "PrivateDevices": "true",
    "ProtectSystem": "strict",
    "ProtectHome": "true",
    "ProtectKernelModules": "true",
    "ProtectKernelTunables": "true",
    "ProtectControlGroups": "true",
    "RestrictNamespaces": "true",
    "RestrictRealtime": "true",
    "RestrictSUIDSGID": "true",
    "LockPersonality": "true",
    "SystemCallArchitectures": "native",
    "CapabilityBoundingSet": "",
}
"""Hardening both units are expected to carry, with the value expected for it."""


def parse_unit(path: Path) -> dict[str, dict[str, list[str]]]:
    """A unit file as ``{section: {key: [values]}}``.

    Values are lists because systemd lets a key repeat -- ``Environment=`` does
    here -- and the repeats are the point rather than noise to collapse.

    Raises:
        AssertionError: if a line is neither a comment, a section nor ``key=value``,
            which is the "does it parse" part of the check.
    """
    sections: dict[str, dict[str, list[str]]] = {}
    current: dict[str, list[str]] | None = None
    for number, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        assert current is not None, f"{path.name}:{number}: {line!r} before any [Section]"
        assert "=" in line, f"{path.name}:{number}: {line!r} is not key=value"
        key, _, value = line.partition("=")
        current.setdefault(key.strip(), []).append(value.strip())
    return sections


def one(section: dict[str, list[str]], key: str) -> str:
    """The single value of a key that is only allowed to appear once."""
    values = section.get(key, [])
    assert len(values) == 1, f"expected one {key}=, found {values}"
    return values[0]


def parse_env_file(path: Path) -> dict[str, str]:
    """A systemd ``EnvironmentFile`` as a mapping, comments dropped."""
    found: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        assert not line.startswith("export "), f"{path.name}: systemd is not a shell: {line!r}"
        assert "=" in line, f"{path.name}: {line!r} is not KEY=value"
        key, _, value = line.partition("=")
        found[key.strip()] = value.strip()
    return found


@pytest.fixture(scope="module")
def bot_unit() -> dict[str, dict[str, list[str]]]:
    return parse_unit(BOT_UNIT)


@pytest.fixture(scope="module")
def backup_unit() -> dict[str, dict[str, list[str]]]:
    return parse_unit(BACKUP_UNIT)


@pytest.fixture(scope="module")
def backup_timer() -> dict[str, dict[str, list[str]]]:
    return parse_unit(BACKUP_TIMER)


# ------------------------------------------------------------- the bot's unit


def test_every_deploy_file_is_there(bot_unit: dict[str, dict[str, list[str]]]) -> None:
    for path in (BOT_UNIT, BACKUP_UNIT, BACKUP_TIMER, ENV_EXAMPLE, BACKUP_SCRIPT):
        assert path.is_file(), f"{path} is named in the README and is missing"
    assert set(bot_unit) == {"Unit", "Service", "Install"}


def test_the_bot_unit_runs_the_bot_as_its_own_user(
    bot_unit: dict[str, dict[str, list[str]]],
) -> None:
    service = bot_unit["Service"]

    assert one(service, "ExecStart") == "/opt/esgbot/.venv/bin/python -m esgbot"
    assert one(service, "User") == "esgbot"
    assert one(service, "Group") == "esgbot"
    assert one(service, "WorkingDirectory") == "/opt/esgbot"
    assert one(service, "Type") == "simple"
    assert one(bot_unit["Install"], "WantedBy") == "multi-user.target"


def test_the_bot_unit_reads_the_token_from_a_file_outside_the_checkout(
    bot_unit: dict[str, dict[str, list[str]]],
) -> None:
    """The token comes from /etc, never from Environment= where `systemctl show`
    and the journal would have it."""
    service = bot_unit["Service"]

    assert one(service, "EnvironmentFile") == "/etc/esgbot/esgbot.env"
    assert not any(TOKEN_ENV_VAR in value for value in service.get("Environment", []))


def test_the_bot_unit_restarts_on_failure_but_gives_up_on_a_loop(
    bot_unit: dict[str, dict[str, list[str]]],
) -> None:
    unit, service = bot_unit["Unit"], bot_unit["Service"]

    assert one(service, "Restart") == "on-failure"
    # Long enough that a network which is not up yet gets a few real chances,
    # and enough tries to cover a slow boot before systemd gives up.
    assert int(one(service, "RestartSec").removesuffix("s")) >= 10
    assert int(one(unit, "StartLimitBurst")) >= 5
    assert int(one(unit, "StartLimitIntervalSec")) >= 300
    # The bot stops itself on SIGTERM; this is only the ceiling before SIGKILL.
    assert int(one(service, "TimeoutStopSec").removesuffix("s")) >= 10


def test_the_bot_unit_keeps_the_database_in_a_private_state_directory(
    bot_unit: dict[str, dict[str, list[str]]],
) -> None:
    service = bot_unit["Service"]

    assert one(service, "StateDirectory") == "esgbot"
    assert one(service, "StateDirectoryMode") == "0700"
    assert one(service, "UMask") == "0077"


def test_the_bot_unit_logs_to_the_journal(bot_unit: dict[str, dict[str, list[str]]]) -> None:
    service = bot_unit["Service"]

    assert one(service, "StandardOutput") == "journal"
    assert one(service, "StandardError") == "journal"
    assert one(service, "SyslogIdentifier") == "esgbot"


def test_the_bot_unit_waits_for_the_network_before_polling(
    bot_unit: dict[str, dict[str, list[str]]],
) -> None:
    unit = bot_unit["Unit"]

    assert "network-online.target" in one(unit, "After")
    assert "network-online.target" in one(unit, "Wants")


@pytest.mark.parametrize(("key", "value"), sorted(HARDENING.items()))
def test_both_units_carry_the_same_hardening(key: str, value: str) -> None:
    for path in (BOT_UNIT, BACKUP_UNIT):
        service = parse_unit(path)["Service"]
        assert one(service, key) == value, f"{path.name} is missing {key}={value}"


def test_the_bot_unit_allows_only_the_address_families_it_needs(
    bot_unit: dict[str, dict[str, list[str]]],
) -> None:
    """Outbound HTTPS to Telegram, and local sockets for name resolution."""
    families = one(bot_unit["Service"], "RestrictAddressFamilies").split()

    assert set(families) == {"AF_UNIX", "AF_INET", "AF_INET6"}


# ---------------------------------------------------------- the backup units


def test_the_backup_unit_is_a_oneshot_that_runs_the_script(
    backup_unit: dict[str, dict[str, list[str]]],
) -> None:
    service = backup_unit["Service"]

    assert one(service, "Type") == "oneshot"
    assert one(service, "ExecStart") == "/opt/esgbot/deploy/backup.sh"
    assert one(service, "User") == "esgbot"
    assert "Install" not in backup_unit, "the timer starts it; it is not enabled itself"


def test_the_backup_unit_is_never_given_the_token(
    backup_unit: dict[str, dict[str, list[str]]],
) -> None:
    """A backup needs a database path and nothing else."""
    service = backup_unit["Service"]

    assert "EnvironmentFile" not in service
    assert TOKEN_ENV_VAR not in BACKUP_UNIT.read_text()
    assert TOKEN_ENV_VAR not in BACKUP_SCRIPT.read_text()


def test_the_backup_unit_says_where_the_database_and_the_copies_are(
    backup_unit: dict[str, dict[str, list[str]]],
) -> None:
    service = backup_unit["Service"]
    environment = dict(value.partition("=")[::2] for value in service["Environment"])

    assert environment[DB_PATH_ENV_VAR].startswith(STATE_DIRECTORY)
    assert int(environment[KEEP_DAYS_ENV_VAR]) > 0
    # The copies go outside the state directory, and systemd will not create that
    # for us, so the unit has to be told it may write there.
    backup_dir = environment[BACKUP_DIR_ENV_VAR]
    assert not backup_dir.startswith(STATE_DIRECTORY)
    writable = [path.removeprefix("-") for path in service["ReadWritePaths"]]
    assert backup_dir in writable
    assert one(service, "StateDirectory") == "esgbot"


def test_the_backup_unit_needs_no_network(
    backup_unit: dict[str, dict[str, list[str]]],
) -> None:
    service = backup_unit["Service"]

    assert one(service, "PrivateNetwork") == "true"
    assert one(service, "RestrictAddressFamilies") == "AF_UNIX"


def test_the_timer_runs_daily_and_catches_up_after_downtime(
    backup_timer: dict[str, dict[str, list[str]]],
) -> None:
    timer = backup_timer["Timer"]

    assert one(timer, "OnCalendar") == "*-*-* 03:30:00"
    assert one(timer, "Persistent") == "true"
    assert one(timer, "RandomizedDelaySec") == "15m"
    assert one(backup_timer["Install"], "WantedBy") == "timers.target"


def test_the_timer_names_the_backup_unit_that_is_actually_here(
    backup_timer: dict[str, dict[str, list[str]]],
) -> None:
    assert one(backup_timer["Timer"], "Unit") == BACKUP_UNIT.name


# ----------------------------------------------------- the environment example


def test_the_example_names_exactly_the_variables_the_bot_reads() -> None:
    """Every variable here is one the code looks up, and none is missing."""
    found = parse_env_file(ENV_EXAMPLE)

    assert set(found) == {TOKEN_ENV_VAR, DB_PATH_ENV_VAR, LEVEL_ENV_VAR}


def test_the_example_carries_a_placeholder_rather_than_a_token() -> None:
    found = parse_env_file(ENV_EXAMPLE)
    token = found[TOKEN_ENV_VAR]

    assert "replace" in token.lower()
    # systemd strips quotes from a value, so an example must not teach them.
    assert '"' not in token
    assert "'" not in token


def test_the_example_and_the_backup_unit_agree_on_the_database(
    backup_unit: dict[str, dict[str, list[str]]],
) -> None:
    """The one path that has to match in two places, so a test holds it there."""
    from_example = parse_env_file(ENV_EXAMPLE)[DB_PATH_ENV_VAR]
    environment = dict(value.partition("=")[::2] for value in backup_unit["Service"]["Environment"])

    assert from_example == environment[DB_PATH_ENV_VAR]
    assert from_example.startswith(f"{STATE_DIRECTORY}/")


# --------------------------------------------------------- systemd's own parse


def systemd_analyze() -> str | None:
    """``systemd-analyze``, if this machine has one worth asking."""
    found = shutil.which("systemd-analyze")
    # Without a running systemd there is no manager for verify to work against,
    # so a machine that only has the binary is one to skip rather than to trust.
    return found if found and Path("/run/systemd/system").is_dir() else None


@pytest.mark.parametrize("name", ["esgbot.service", "esgbot-backup.service", "esgbot-backup.timer"])
def test_systemd_itself_accepts_the_units(name: str, tmp_path: Path) -> None:
    analyze = systemd_analyze()
    if analyze is None:
        pytest.skip("systemd-analyze and a running systemd are needed for this one")
    stand_in = shutil.which("true")
    if stand_in is None:
        pytest.skip("no /bin/true to stand in for a command that lives on the server")

    for unit in (BOT_UNIT, BACKUP_UNIT, BACKUP_TIMER):
        lines = [
            f"ExecStart={stand_in}" if line.startswith("ExecStart=") else line
            for line in unit.read_text().splitlines()
        ]
        (tmp_path / unit.name).write_text("\n".join(lines) + "\n")

    done = subprocess.run(
        [analyze, "verify", str(tmp_path / name)],
        capture_output=True,
        text=True,
        check=False,
    )

    report = done.stdout + done.stderr
    assert done.returncode == 0, report
    # A misspelled directive is only a warning, and a warning is how hardening
    # goes missing without anyone noticing.
    assert "Unknown key" not in report, report
    assert "Failed to parse" not in report, report


# ------------------------------------------------------------ the backup script


def test_the_backup_script_can_be_run_by_systemd() -> None:
    assert os.access(BACKUP_SCRIPT, os.X_OK), "ExecStart= needs the executable bit"
    text = BACKUP_SCRIPT.read_text()
    assert text.startswith("#!")
    assert "set -euo pipefail" in text
    assert "-m esgbot.backup" in text


def bash() -> str | None:
    return shutil.which("bash")


def run_script(
    arguments: list[str], environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run ``deploy/backup.sh`` the way the unit does, with a clean environment."""
    return subprocess.run(
        [str(BACKUP_SCRIPT), *arguments],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **environment},
    )


def test_the_script_backs_up_a_live_database(tmp_path: Path) -> None:
    """End to end, as the timer runs it: a WAL database in, a sound copy out."""
    if bash() is None:
        pytest.skip("no bash to run the script with")
    database = tmp_path / "esgbot.sqlite3"
    connection = sqlite3.connect(database, isolation_level=None)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA wal_autocheckpoint = 0")
    connection.execute("CREATE TABLE flips (n INTEGER)")
    connection.execute("INSERT INTO flips VALUES (1)")
    backups = tmp_path / "backups"

    try:
        done = run_script(
            [],
            {
                "ESGBOT_PYTHON": sys.executable,
                DB_PATH_ENV_VAR: str(database),
                BACKUP_DIR_ENV_VAR: str(backups),
                KEEP_DAYS_ENV_VAR: "14",
            },
        )
    finally:
        connection.close()

    assert done.returncode == 0, done.stdout + done.stderr
    (copy,) = list(backups.iterdir())
    reader = sqlite3.connect(copy)
    try:
        assert [str(row[0]) for row in reader.execute("PRAGMA integrity_check")] == ["ok"]
        assert reader.execute("SELECT count(*) FROM flips").fetchone()[0] == 1
    finally:
        reader.close()


def test_the_script_says_what_to_do_when_there_is_no_environment_to_run_in(
    tmp_path: Path,
) -> None:
    if bash() is None:
        pytest.skip("no bash to run the script with")

    done = run_script([], {"ESGBOT_PYTHON": str(tmp_path / "no-such-python")})

    assert done.returncode == 1
    assert "uv sync --no-dev" in done.stderr
    assert "ESGBOT_PYTHON" in done.stderr
