#!/usr/bin/env python3
"""Windrose Server Telegram Bot."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

load_dotenv()


def _require_env(key: str) -> str:
    value = os.getenv(key, "").strip()
    if not value:
        raise ValueError(f"Required environment variable '{key}' is not set in .env")
    return value


def _parse_int_env(key: str, default: int) -> int:
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"Environment variable '{key}' must be an integer, got: '{raw}'")


def _parse_bool_env(key: str, default: bool) -> bool:
    raw = os.getenv(key, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


BOT_TOKEN: str = _require_env("BOT_TOKEN")
ADMIN_ID: int = _parse_int_env("ADMIN_ID", 0)
if not ADMIN_ID:
    raise ValueError("Required environment variable 'ADMIN_ID' is not set in .env")

COMPOSE_DIR: str = os.getenv("COMPOSE_DIR", "/opt/windrose").strip()
LOG_LINES: int = _parse_int_env("LOG_LINES", 40)
MAX_PLAYERS: int = _parse_int_env("MAX_PLAYERS", 4)
NOTIFY_ADMIN_ON_START: bool = _parse_bool_env("NOTIFY_ADMIN_ON_START", True)
LOG_WATCHER_DEBUG: bool = _parse_bool_env("LOG_WATCHER_DEBUG", False)

_trusted_raw = os.getenv("TRUSTED_USERS", "")
TRUSTED_USERS: frozenset[int] = frozenset(
    int(uid.strip()) for uid in _trusted_raw.split(",") if uid.strip().isdigit()
)


# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────

_log_handlers: list[logging.Handler] = [logging.StreamHandler()]

_log_file = "/var/log/windrose-bot.log"
try:
    _log_handlers.append(logging.FileHandler(_log_file, encoding="utf-8"))
except OSError as _e:
    print(f"[WARN] Cannot open log file {_log_file}: {_e}", file=sys.stderr)

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.DEBUG if LOG_WATCHER_DEBUG else logging.INFO,
    handlers=_log_handlers,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Concurrency
# ─────────────────────────────────────────────

_server_lock = asyncio.Lock()


# ─────────────────────────────────────────────
# Access helpers
# ─────────────────────────────────────────────


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def is_trusted(user_id: int) -> bool:
    return user_id in TRUSTED_USERS or is_admin(user_id)


def user_label(update: Update) -> str:
    u = update.effective_user
    name = u.full_name if u else "Unknown"
    uid = u.id if u else 0
    return f"{name} (id={uid})"


# ─────────────────────────────────────────────
# MarkdownV2 utilities
# ─────────────────────────────────────────────

_MD2_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def md2(text: str) -> str:
    """Escape arbitrary text for MarkdownV2."""
    return _MD2_SPECIAL.sub(r"\\\1", text)


def md2_code_block(text: str, max_len: int = 3500) -> str:
    """Escape text for use inside a triple-backtick code block."""
    if len(text) > max_len:
        text = "…(truncated)\n" + text[-max_len:]
    text = text.replace("\\", "\\\\").replace("`", "\\`")
    return text


def now_str() -> str:
    return md2(datetime.now().strftime("%d.%m.%Y %H:%M"))


# ─────────────────────────────────────────────
# Docker compose operations
# ─────────────────────────────────────────────

_ALLOWED_COMMANDS: dict[str, list[list[str]]] = {
    "ps_running": [["docker", "compose", "ps", "--status", "running", "-q"]],
    "ps_ids":     [["docker", "compose", "ps", "-q"]],
    "start":      [["docker", "compose", "up", "-d"]],
    "stop":       [["docker", "compose", "stop"]],
    "restart":    [["docker", "compose", "restart"]],
    "pull":       [["docker", "compose", "pull"]],
    "update":     [
        ["docker", "compose", "pull"],
        ["docker", "compose", "up", "-d"],
    ],
    "logs":       [["docker", "compose", "logs", "--tail", str(LOG_LINES), "--no-color"]],
    "logs_seed":  [["docker", "compose", "logs", "--tail", "5000", "--no-color"]],
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHF]")


def _run(cmd: list[str], timeout: int = 180) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            cmd,
            cwd=COMPOSE_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "⏱ Command timed out."
    except FileNotFoundError as exc:
        return False, f"Command not found: {exc}"
    except Exception as exc:
        logger.exception("Unexpected error running: %s", shlex.join(cmd))
        return False, f"Unexpected error: {exc}"


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def server_is_running() -> bool:
    ok, out = _run(_ALLOWED_COMMANDS["ps_running"][0])
    return ok and bool(out.strip())


def server_uptime() -> Optional[str]:
    try:
        ids_result = subprocess.run(
            _ALLOWED_COMMANDS["ps_ids"][0],
            cwd=COMPOSE_DIR,
            capture_output=True,
            text=True,
            timeout=10,
        )
        container_ids = ids_result.stdout.strip().splitlines()
        if not container_ids:
            return None

        inspect_result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.StartedAt}}", container_ids[0]],
            capture_output=True,
            text=True,
            timeout=10,
        )
        started_at = inspect_result.stdout.strip()
        if not started_at or started_at == "0001-01-01T00:00:00Z":
            return None

        dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        delta = int((datetime.now(tz=timezone.utc) - dt).total_seconds())
        return _fmt_duration(delta)

    except Exception as exc:
        logger.warning("Failed to get uptime: %s", exc)
        return None


def start_server() -> tuple[bool, str]:
    return _run(_ALLOWED_COMMANDS["start"][0], timeout=60)


def stop_server() -> tuple[bool, str]:
    return _run(_ALLOWED_COMMANDS["stop"][0], timeout=60)


def restart_server() -> tuple[bool, str]:
    return _run(_ALLOWED_COMMANDS["restart"][0], timeout=90)


def pull_only() -> tuple[bool, str]:
    return _run(_ALLOWED_COMMANDS["pull"][0], timeout=300)


def update_server() -> tuple[bool, str]:
    ok1, out1 = _run(_ALLOWED_COMMANDS["update"][0], timeout=300)
    if not ok1:
        return False, f"Pull failed:\n{out1}"
    ok2, out2 = _run(_ALLOWED_COMMANDS["update"][1], timeout=120)
    return ok2, f"📥 Pull:\n{out1}\n\n🔄 Recreate:\n{out2}"


def get_logs() -> str:
    ok, out = _run(_ALLOWED_COMMANDS["logs"][0])
    if not ok or not out.strip():
        return "Logs unavailable or empty."
    return _strip_ansi(out)


def get_logs_for_seed() -> str:
    ok, out = _run(_ALLOWED_COMMANDS["logs_seed"][0], timeout=30)
    return _strip_ansi(out) if ok else ""


# ─────────────────────────────────────────────
# Player tracking — event-based parser
# ─────────────────────────────────────────────

# Server logs Account connected and Join succeeded close in time but in either
# order (observed both sequences in production). We pair them inside a 5-second
# window. Both events are also emitted twice through different code paths, so we
# dedup each event for 5 seconds before pairing.

_JOIN_SUCCEEDED_RE = re.compile(r"LogNet: Join succeeded: (?P<name>.+?)\s*$")

_ACCOUNT_CONNECTED_RE = re.compile(
    r"R5LogDataKeeper.*Account connected\. AccountId (?P<account_id>[0-9A-Fa-f]+)"
)

_ACCOUNT_DISCONNECTED_RE = re.compile(
    r"R5LogCoopProxy.*OnAccountDisconnected.*AccountId (?P<account_id>[0-9A-Fa-f]+)"
)

_PLAYER_DUMP_RE = re.compile(
    r"\d+\.\s+Name '(?P<name>[^']+)'\.\s+"
    r"AccountId '(?P<account_id>[0-9A-Fa-f]+)'\.\s+"
    r"State '(?P<state>[^']+)'\."
)

_ONLINE_STATES = {"WaitingForClientIsReady", "ReadyToPlay"}

_EVENT_DEDUP_WINDOW_SEC = 5.0
_JOIN_PAIR_TIMEOUT_SEC = 5.0
_WATCHER_WARMUP_SEC = 3.0


_active_players: dict[str, dict] = {}

# Pending halves of a join event waiting to be paired. Each entry is one of:
#   {"kind": "id", "value": account_id, "ts": monotonic}
#   {"kind": "name", "value": player_name, "ts": monotonic}
_pending_join_halves: list[dict] = []

_recent_events: dict[tuple[str, str], float] = {}

_watcher_task: Optional[asyncio.Task] = None
_watcher_started_at: float = 0.0
_lines_seen_in_warmup: int = 0


def _is_event_duplicate(event_type: str, key: str) -> bool:
    """Return True if this event was emitted within the dedup window."""
    now = time.monotonic()
    cache_key = (event_type, key)
    last = _recent_events.get(cache_key)
    if last is not None and (now - last) < _EVENT_DEDUP_WINDOW_SEC:
        return True
    _recent_events[cache_key] = now
    if len(_recent_events) > 64:
        cutoff = now - _EVENT_DEDUP_WINDOW_SEC
        for k in list(_recent_events.keys()):
            if _recent_events[k] < cutoff:
                _recent_events.pop(k, None)
    return False


def _parse_duration_secs(s: str) -> Optional[int]:
    """Parse '+HH:MM:SS.mmm' or '+D.HH:MM:SS.mmm' into seconds. None on parse error."""
    s = s.lstrip("+")
    try:
        dot_pos = s.find(".")
        colon_pos = s.find(":")
        if dot_pos != -1 and (colon_pos == -1 or dot_pos < colon_pos):
            days = int(s[:dot_pos])
            time_part = s[dot_pos + 1:]
        else:
            days = 0
            time_part = s

        time_part = time_part.split(".")[0]
        h, m, sec_str = time_part.split(":")
        return days * 86400 + int(h) * 3600 + int(m) * 60 + int(sec_str)
    except Exception:
        return None


def _fmt_duration(secs: Optional[int]) -> str:
    if secs is None:
        return "unknown"
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        m, s = divmod(secs, 60)
        return f"{m}m {s}s"
    if secs < 86400:
        h, rem = divmod(secs, 3600)
        return f"{h}h {rem // 60}m"
    d, rem = divmod(secs, 86400)
    return f"{d}d {rem // 3600}h"


def _online_summary() -> str:
    n = len(_active_players)
    if n == 0:
        return f"0/{MAX_PLAYERS} (no one online)"
    names = ", ".join(info["name"] for info in _active_players.values())
    return f"{n}/{MAX_PLAYERS}: {names}"


def _build_join_notification(name: str) -> str:
    return (
        f"🟢 *Player joined*\n"
        f"\n"
        f"👤 {md2(name)}\n"
        f"🕐 {now_str()}\n"
        f"\n"
        f"👥 Online: {md2(_online_summary())}"
    )


def _build_leave_notification(name: str, time_in_game_secs: Optional[int]) -> str:
    time_str = md2(_fmt_duration(time_in_game_secs))
    hint = "\n💤 *Server can be stopped*" if not _active_players else ""
    return (
        f"🔴 *Player left*\n"
        f"\n"
        f"👤 {md2(name)}\n"
        f"⏱ Time in game: {time_str}\n"
        f"🕐 {now_str()}\n"
        f"\n"
        f"👥 Online: {md2(_online_summary())}"
        f"{hint}"
    )


def _seed_active_players_from_logs(log_text: str) -> None:
    """Reconstruct _active_players from the most recent table dump in the logs."""
    if not log_text:
        return

    lines = log_text.splitlines()
    last_connected_idx = -1
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].rstrip().endswith("Connected Accounts"):
            last_connected_idx = i
            break

    if last_connected_idx == -1:
        logger.info("No 'Connected Accounts' block in seed logs.")
        return

    seeded: dict[str, dict] = {}
    for line in lines[last_connected_idx + 1:]:
        stripped = line.rstrip()
        if stripped.endswith("Reserved Accounts") or stripped.endswith("Disconnected Accounts"):
            break
        m = _PLAYER_DUMP_RE.search(line)
        if not m:
            continue
        if m.group("state") not in _ONLINE_STATES:
            continue
        seeded[m.group("account_id")] = {
            "name": m.group("name"),
            "join_time": datetime.now(),
        }

    if seeded:
        _active_players.update(seeded)
        logger.info(
            "Seeded %d active player(s) from logs: %s",
            len(seeded),
            ", ".join(p["name"] for p in seeded.values()),
        )
    else:
        logger.info("No active players to seed from logs.")


def _try_pair_join(new_kind: str, new_value: str) -> Optional[tuple[str, str]]:
    """
    Try to pair a freshly-seen Join half with one already pending.

    new_kind: "id" or "name"
    new_value: the account_id or player name we just observed.

    Returns (account_id, name) if a complementary half is pending; otherwise
    stores the new half and returns None. Stale entries are dropped on each call.
    """
    now = time.monotonic()
    _pending_join_halves[:] = [
        h for h in _pending_join_halves if (now - h["ts"]) < _JOIN_PAIR_TIMEOUT_SEC
    ]

    other_kind = "name" if new_kind == "id" else "id"
    for i, half in enumerate(_pending_join_halves):
        if half["kind"] == other_kind:
            other_value = half["value"]
            _pending_join_halves.pop(i)
            if new_kind == "id":
                return new_value, other_value
            else:
                return other_value, new_value

    _pending_join_halves.append({"kind": new_kind, "value": new_value, "ts": now})
    return None


async def _emit_join(app: Application, account_id: str, name: str) -> None:
    if account_id in _active_players:
        logger.debug("Join for %s (%s) ignored — already active", name, account_id)
        return
    _active_players[account_id] = {
        "name": name,
        "join_time": datetime.now(),
    }
    text = _build_join_notification(name)
    try:
        await app.bot.send_message(ADMIN_ID, text, parse_mode=ParseMode.MARKDOWN_V2)
        logger.info("Player JOIN: %s (%s)", name, account_id)
    except Exception as exc:
        logger.warning("Failed to send join notification: %s", exc)


async def _emit_leave(app: Application, account_id: str) -> None:
    info = _active_players.pop(account_id, None)
    if info is None:
        logger.debug("Leave for unknown account_id %s — skipping", account_id)
        return
    name = info["name"]
    time_in_game = int((datetime.now() - info["join_time"]).total_seconds())
    text = _build_leave_notification(name, time_in_game)
    try:
        await app.bot.send_message(ADMIN_ID, text, parse_mode=ParseMode.MARKDOWN_V2)
        logger.info("Player LEAVE: %s (%s) time=%ds", name, account_id, time_in_game)
    except Exception as exc:
        logger.warning("Failed to send leave notification: %s", exc)


async def _handle_log_line(app: Application, line: str) -> None:
    global _lines_seen_in_warmup

    in_warmup = (time.monotonic() - _watcher_started_at) < _WATCHER_WARMUP_SEC

    if LOG_WATCHER_DEBUG and _lines_seen_in_warmup < 20:
        logger.debug("WATCHER_RAW: %s", line[:300])
        _lines_seen_in_warmup += 1

    if (m := _ACCOUNT_CONNECTED_RE.search(line)):
        account_id = m.group("account_id")
        if _is_event_duplicate("connected", account_id):
            return
        if in_warmup:
            return
        logger.debug("Account connected: %s", account_id)
        pair = _try_pair_join("id", account_id)
        if pair is not None:
            await _emit_join(app, pair[0], pair[1])
        return

    if (m := _JOIN_SUCCEEDED_RE.search(line)):
        name = m.group("name")
        if _is_event_duplicate("join_succeeded", name):
            return
        if in_warmup:
            return
        logger.debug("Join succeeded: %s", name)
        pair = _try_pair_join("name", name)
        if pair is not None:
            await _emit_join(app, pair[0], pair[1])
        return

    if (m := _ACCOUNT_DISCONNECTED_RE.search(line)):
        account_id = m.group("account_id")
        if _is_event_duplicate("disconnected", account_id):
            return
        if in_warmup:
            return
        await _emit_leave(app, account_id)


async def _log_watcher(app: Application) -> None:
    """Tail docker logs and dispatch player events. Auto-restarts on subprocess failure."""
    global _watcher_started_at, _lines_seen_in_warmup

    seed_logs = await asyncio.get_running_loop().run_in_executor(None, get_logs_for_seed)
    _seed_active_players_from_logs(seed_logs)

    cmd = ["docker", "compose", "logs", "--tail", "0", "-f", "--no-color"]
    logger.info("Log watcher starting. Command: %s", shlex.join(cmd))

    while True:
        proc: Optional[asyncio.subprocess.Process] = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=COMPOSE_DIR,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            _watcher_started_at = time.monotonic()
            _lines_seen_in_warmup = 0
            logger.info("Log watcher: subprocess PID=%s", proc.pid)

            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n").rstrip()
                if not line:
                    continue
                await _handle_log_line(app, line)

            await proc.wait()
            logger.warning(
                "Log watcher: subprocess exited (rc=%s), retrying in 5s",
                proc.returncode,
            )
            _active_players.clear()
            _pending_join_halves.clear()

        except asyncio.CancelledError:
            logger.info("Log watcher: cancelled, shutting down subprocess")
            if proc is not None and proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    proc.kill()
            raise

        except Exception as exc:
            logger.exception("Log watcher: unexpected error (%s), retrying in 5s", exc)

        await asyncio.sleep(5)


# ─────────────────────────────────────────────
# Self-test (runs at startup)
# ─────────────────────────────────────────────


_SELF_TEST_FIXTURES = {
    "join_succeeded": (
        "windrose  | [2026.05.10-14.09.13:587][203]LogNet: Join succeeded: PirateEscort",
        _JOIN_SUCCEEDED_RE,
        {"name": "PirateEscort"},
    ),
    "account_connected": (
        "windrose  | [2026.05.10-14.09.12:892][203]R5LogDataKeeper:              "
        "[1918203] ...erForServer::OnPlayerStateReplicateAccountId   "
        "Account connected. AccountId F4EDF8124173531F31F43494D3CD2C2A",
        _ACCOUNT_CONNECTED_RE,
        {"account_id": "F4EDF8124173531F31F43494D3CD2C2A"},
    ),
    "account_disconnected": (
        "windrose  | [2026.05.10-14.09.37:883][936]R5LogCoopProxy:               "
        "[1918927] UR5CoopProxyServer::OnAccountDisconnected         "
        "Account disconnected. Inform Cm. AccountId F4EDF8124173531F31F43494D3CD2C2A. "
        "BLPlayerSessionId 1e3ff8ea517540bc8dbe2a9ca0188908. "
        "DisconnectReason 'BL disconnected'. FarewellReason 'Go to lobby'",
        _ACCOUNT_DISCONNECTED_RE,
        {"account_id": "F4EDF8124173531F31F43494D3CD2C2A"},
    ),
    "player_dump_active": (
        "     1. Name 'PirateEscort'. AccountId 'F4EDF8124173531F31F43494D3CD2C2A'. "
        "State 'WaitingForClientIsReady'. NetAddress 'R5:b97426ff'. UePortal ''. "
        "ReserveMoment 2026.05.10-11.31.45. Connected in +00:01:27.507.",
        _PLAYER_DUMP_RE,
        {
            "name": "PirateEscort",
            "account_id": "F4EDF8124173531F31F43494D3CD2C2A",
            "state": "WaitingForClientIsReady",
        },
    ),
}


def _run_self_test() -> bool:
    """Validate parser regexes against known-good fixtures. Returns False on failure."""
    failed: list[str] = []

    for label, (line, regex, expected) in _SELF_TEST_FIXTURES.items():
        match = regex.search(line)
        if match is None:
            failed.append(f"  - {label}: regex did not match")
            continue
        actual = match.groupdict()
        for key, want in expected.items():
            if actual.get(key) != want:
                failed.append(
                    f"  - {label}: group '{key}' = {actual.get(key)!r}, want {want!r}"
                )

    duration_cases = [
        ("+00:00:13", 13),
        ("+00:01:27.507", 87),
        ("+01:46:17.213", 6377),
        ("+1.02:36:53.078", 95813),
    ]
    for raw, want in duration_cases:
        got = _parse_duration_secs(raw)
        if got != want:
            failed.append(f"  - duration {raw!r}: got {got}, want {want}")

    if failed:
        logger.error("Self-test failed:\n%s", "\n".join(failed))
        return False

    logger.info("Self-test passed (%d fixtures).", len(_SELF_TEST_FIXTURES))
    return True


# ─────────────────────────────────────────────
# Keyboards
# ─────────────────────────────────────────────


def admin_keyboard(running: bool) -> InlineKeyboardMarkup:
    if running:
        row1 = [
            InlineKeyboardButton("🔴 Stop", callback_data="stop"),
            InlineKeyboardButton("🔄 Restart", callback_data="restart"),
        ]
    else:
        row1 = [InlineKeyboardButton("🟢 Start", callback_data="start")]

    return InlineKeyboardMarkup([
        row1,
        [
            InlineKeyboardButton("⬆️ Pull", callback_data="pull_only"),
            InlineKeyboardButton("⬆️🟢 Pull & Start", callback_data="update"),
        ],
        [
            InlineKeyboardButton("👥 Online", callback_data="online"),
            InlineKeyboardButton("📊 Status", callback_data="status"),
        ],
        [
            InlineKeyboardButton("📋 Logs", callback_data="logs"),
        ],
    ])


def trusted_keyboard(running: bool) -> InlineKeyboardMarkup:
    if running:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("👥 Online", callback_data="online"),
                InlineKeyboardButton("📊 Status", callback_data="status"),
            ],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Start server", callback_data="start")],
        [InlineKeyboardButton("📊 Status", callback_data="status")],
    ])


def keyboard_for(user_id: int, running: bool) -> InlineKeyboardMarkup:
    return admin_keyboard(running) if is_admin(user_id) else trusted_keyboard(running)


# ─────────────────────────────────────────────
# Status / online / players text
# ─────────────────────────────────────────────


def status_text() -> str:
    running = server_is_running()
    uptime = server_uptime() if running else None
    state = "🟢 *Running*" if running else "🔴 *Stopped*"

    lines = [
        "📊 *Windrose server status*",
        "",
        f"State: {state}",
    ]
    if uptime:
        lines.append(f"Uptime: `{md2(uptime)}`")
    if running:
        lines.append(f"Players online: `{md2(str(len(_active_players)))}/{MAX_PLAYERS}`")
    lines += ["", f"🕐 {now_str()}"]
    return "\n".join(lines)


def online_text() -> str:
    if not server_is_running():
        return f"📴 *Server is stopped*\n\n🕐 {now_str()}"

    n = len(_active_players)
    lines = [
        "👥 *Online*",
        "",
        f"In game: `{md2(str(n))}/{MAX_PLAYERS}`",
    ]
    if n > 0:
        lines.append("")
        now = datetime.now()
        for info in sorted(_active_players.values(), key=lambda p: p["join_time"]):
            elapsed = int((now - info["join_time"]).total_seconds())
            lines.append(f"• {md2(info['name'])} — `{md2(_fmt_duration(elapsed))}`")
    else:
        lines += ["", "_No one is in the game_"]
    lines += ["", f"🕐 {now_str()}"]
    return "\n".join(lines)


def players_admin_text() -> str:
    if not server_is_running():
        return "📴 Server is stopped — no player data\\."

    if not _active_players:
        return f"👥 *Players*\n\n_No one is in the game_\n\n🕐 {now_str()}"

    lines = ["👥 *Players*", ""]
    now = datetime.now()
    for account_id, info in sorted(_active_players.items(), key=lambda kv: kv[1]["join_time"]):
        elapsed = int((now - info["join_time"]).total_seconds())
        short_id = account_id[:8]
        lines.append(
            f"• *{md2(info['name'])}*\n"
            f"  ID: `{md2(short_id)}…`\n"
            f"  In game: `{md2(_fmt_duration(elapsed))}`"
        )
    lines += ["", f"🕐 {now_str()}"]
    return "\n".join(lines)


# ─────────────────────────────────────────────
# Admin notifications
# ─────────────────────────────────────────────


async def _notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    try:
        await context.bot.send_message(ADMIN_ID, text, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as exc:
        logger.warning("Failed to notify admin: %s", exc)


def _start_notify_text(full_name: str, user_id: int, ok: bool, out: str) -> str:
    safe_name = md2(full_name)
    result = "✅ ok" if ok else f"❌ error:\n```\n{md2_code_block(out, 300)}\n```"
    return (
        f"🔔 *{safe_name}* \\(id: `{user_id}`\\) started the server — {result}\n"
        f"🕐 {now_str()}"
    )


# ─────────────────────────────────────────────
# Command handlers
# ─────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not is_trusted(uid):
        logger.info("Ignored /start from unknown user %s", user_label(update))
        return

    running = server_is_running()

    if is_admin(uid):
        text = (
            "👾 *Windrose Server Bot*\n"
            "\n"
            "Available commands:\n"
            "`/start` — this menu\n"
            "`/status` — server status\n"
            "`/players` — player list\n"
            "`/logs` — recent logs\n"
        )
    else:
        text = (
            "👾 *Windrose Server Bot*\n"
            "\n"
            "Use the buttons below to manage the server\\.\n"
        )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=keyboard_for(uid, running),
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not is_trusted(uid):
        return

    running = server_is_running()
    await update.message.reply_text(
        status_text(),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=keyboard_for(uid, running),
    )


async def cmd_players(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not is_admin(uid):
        return

    await update.message.reply_text(
        players_admin_text(),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=admin_keyboard(server_is_running()),
    )


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not is_admin(uid):
        return

    msg = await update.message.reply_text("⏳ Fetching logs…")
    logs = await asyncio.get_running_loop().run_in_executor(None, get_logs)
    chunk = md2_code_block(logs)
    await msg.edit_text(
        f"📋 *Server logs* \\(last {LOG_LINES} lines\\):\n\n```\n{chunk}\n```",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=admin_keyboard(server_is_running()),
    )


# ─────────────────────────────────────────────
# Callback dispatcher
# ─────────────────────────────────────────────


_TRUSTED_ACTIONS: frozenset[str] = frozenset({"start", "status", "online"})


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    uid = query.from_user.id
    action = query.data

    if not is_trusted(uid):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    if not is_admin(uid) and action not in _TRUSTED_ACTIONS:
        await query.answer("⛔ Insufficient permissions.", show_alert=True)
        return

    await query.answer()
    logger.info("Action '%s' requested by %s", action, user_label(update))

    if action == "status":
        await query.edit_message_text(
            status_text(),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=keyboard_for(uid, server_is_running()),
        )
        return

    if action == "online":
        await query.edit_message_text(
            online_text(),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=keyboard_for(uid, server_is_running()),
        )
        return

    if action == "start":
        await _action_start(query, context, uid)
        return

    if _server_lock.locked():
        await query.answer("⏳ Operation in progress, please wait.", show_alert=True)
        return

    async with _server_lock:
        if action == "stop":
            await _action_stop(query)
        elif action == "restart":
            await _action_restart(query)
        elif action == "pull_only":
            await _action_pull(query)
        elif action == "update":
            await _action_update(query)
        elif action == "logs":
            await _action_logs(query)


async def _action_start(query, context: ContextTypes.DEFAULT_TYPE, uid: int) -> None:
    if _server_lock.locked():
        await query.answer("⏳ Operation in progress, please wait.", show_alert=True)
        return

    async with _server_lock:
        if server_is_running():
            await query.edit_message_text(
                "✅ Server is already running\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=keyboard_for(uid, True),
            )
            return

        await query.edit_message_text(
            "⏳ Starting server\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2
        )
        ok, out = await asyncio.get_running_loop().run_in_executor(None, start_server)

        if NOTIFY_ADMIN_ON_START and not is_admin(uid):
            await _notify_admin(
                context,
                _start_notify_text(query.from_user.full_name, uid, ok, out),
            )

        if ok:
            text = "✅ *Server started\\!*"
        else:
            text = f"❌ *Start failed:*\n```\n{md2_code_block(out, 600)}\n```"

        await query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=keyboard_for(uid, server_is_running()),
        )


async def _action_stop(query) -> None:
    await query.edit_message_text(
        "⏳ Stopping server\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2
    )
    ok, out = await asyncio.get_running_loop().run_in_executor(None, stop_server)
    text = (
        "🔴 *Server stopped\\.*"
        if ok
        else f"❌ *Error:*\n```\n{md2_code_block(out, 600)}\n```"
    )
    await query.edit_message_text(
        text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=admin_keyboard(False)
    )


async def _action_restart(query) -> None:
    await query.edit_message_text(
        "⏳ Restarting server\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2
    )
    ok, out = await asyncio.get_running_loop().run_in_executor(None, restart_server)
    text = (
        "🔄 *Server restarted\\.*"
        if ok
        else f"❌ *Error:*\n```\n{md2_code_block(out, 600)}\n```"
    )
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=admin_keyboard(server_is_running()),
    )


async def _action_pull(query) -> None:
    await query.edit_message_text(
        "⏳ Pulling new image\\.\\.\\.\nThis may take a few minutes\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    ok, out = await asyncio.get_running_loop().run_in_executor(None, pull_only)
    status_str = (
        "✅ *Image updated\\.* Server was not started\\."
        if ok
        else "❌ *Pull failed\\.*"
    )
    await query.edit_message_text(
        f"{status_str}\n\n```\n{md2_code_block(out)}\n```",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=admin_keyboard(server_is_running()),
    )


async def _action_update(query) -> None:
    await query.edit_message_text(
        "⏳ Updating server \\(pull \\+ recreate\\)\\.\\.\\.\nThis may take a few minutes\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    ok, out = await asyncio.get_running_loop().run_in_executor(None, update_server)
    status_str = "✅ *Update complete\\!*" if ok else "❌ *Update failed\\.*"
    await query.edit_message_text(
        f"{status_str}\n\n```\n{md2_code_block(out)}\n```",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=admin_keyboard(server_is_running()),
    )


async def _action_logs(query) -> None:
    await query.edit_message_text(
        "⏳ Fetching logs\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2
    )
    logs = await asyncio.get_running_loop().run_in_executor(None, get_logs)
    await query.edit_message_text(
        f"📋 *Logs* \\(last {LOG_LINES} lines\\):\n\n```\n{md2_code_block(logs)}\n```",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=admin_keyboard(server_is_running()),
    )


async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    if not is_trusted(uid):
        return


# ─────────────────────────────────────────────
# Lifecycle
# ─────────────────────────────────────────────


async def post_init(app: Application) -> None:
    global _watcher_task

    await app.bot.set_my_commands(
        [
            BotCommand("start", "Main menu"),
            BotCommand("status", "Server status"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Main menu"),
            BotCommand("status", "Server status"),
            BotCommand("players", "Player list"),
            BotCommand("logs", "Recent logs"),
        ],
        scope=BotCommandScopeChat(chat_id=ADMIN_ID),
    )
    logger.info("Bot commands registered.")

    _watcher_task = asyncio.create_task(_log_watcher(app), name="log_watcher")
    logger.info("Log watcher task created.")

    try:
        await app.bot.send_message(
            ADMIN_ID,
            "🤖 *Windrose Bot started* and ready\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as exc:
        logger.warning("Failed to notify admin on startup: %s", exc)


async def post_shutdown(app: Application) -> None:
    global _watcher_task

    if _watcher_task is not None and not _watcher_task.done():
        _watcher_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(_watcher_task), timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        logger.info("Log watcher task stopped.")

    import json
    import urllib.request

    text = "🛑 *Windrose Bot stopped\\.*"
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": ADMIN_ID,
        "text": text,
        "parse_mode": "MarkdownV2",
    }).encode()

    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                logger.warning("Shutdown notify: HTTP %s", resp.status)
    except Exception as exc:
        logger.warning("Failed to notify admin on shutdown: %s", exc)


def main() -> None:
    logger.info(
        "Starting Windrose Bot | admin=%d | trusted=%s | compose_dir=%s | "
        "max_players=%d | watcher_debug=%s",
        ADMIN_ID,
        TRUSTED_USERS,
        COMPOSE_DIR,
        MAX_PLAYERS,
        LOG_WATCHER_DEBUG,
    )

    if not _run_self_test():
        logger.error("Aborting startup due to self-test failure.")
        sys.exit(1)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("players", cmd_players))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.ALL, handle_unknown))

    logger.info("Bot ready, starting polling…")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        close_loop=False,
    )


if __name__ == "__main__":
    main()
