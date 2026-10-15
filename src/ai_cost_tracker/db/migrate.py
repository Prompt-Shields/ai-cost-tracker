"""Run Alembic migrations from code (CLI ``act migrate``, tests, demo seeding).

Uses the repo-root ``alembic.ini`` when it exists (source checkout) and falls back to an
in-memory config when the package is installed without it. ``script_location`` is always
set to the migrations directory inside the package, so both cases find the same scripts.
"""

import asyncio
import threading
from pathlib import Path

from alembic import command
from alembic.config import Config

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_REPO_ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"


def _config(url: str | None) -> Config:
    ini = _REPO_ALEMBIC_INI if _REPO_ALEMBIC_INI.is_file() else None
    cfg = Config(str(ini) if ini else None)
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    # Don't let alembic.ini's logging config reconfigure the host application.
    cfg.attributes["configure_logging"] = False
    if url:
        # Config values go through ConfigParser interpolation; escape '%' in passwords.
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def _run(fn, *args) -> None:
    """env.py calls asyncio.run(); inside a running loop, do that on a separate thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        fn(*args)
        return
    errors: list[BaseException] = []

    def target() -> None:
        try:
            fn(*args)
        except BaseException as exc:  # re-raised in the caller's thread
            errors.append(exc)

    thread = threading.Thread(target=target, name="alembic-migrate")
    thread.start()
    thread.join()
    if errors:
        raise errors[0]


def upgrade(url: str | None = None, revision: str = "head") -> None:
    """Migrate to ``revision`` (default head). ``url`` defaults to ACT_DATABASE_URL."""
    _run(command.upgrade, _config(url), revision)


def downgrade(url: str | None, revision: str) -> None:
    """Migrate down to ``revision`` (e.g. ``"base"``)."""
    _run(command.downgrade, _config(url), revision)
