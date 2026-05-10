# Windrose Server Telegram Bot

Telegram bot for managing a self-hosted Windrose dedicated server (`indifferentbroccoli/windrose-server-docker`) running under Docker Compose.

## Features

- Server lifecycle: start / stop / restart / pull / pull-and-restart
- Live status with uptime and online player count
- Player join / leave notifications to admin
- `/players` — detailed list with name, account ID and time-in-game (admin)
- `👥 Online` button — quick online check for any trusted user
- `/logs` — last N lines of `docker compose logs` (admin)
- Tiered access: admin (full control) and trusted users (start + view)
- Self-test on startup — parser regexes are validated against fixtures and the bot refuses to start if any fail
- Optional verbose `LOG_WATCHER_DEBUG` mode for diagnosing parser issues live in `journalctl`

<details>
   <summary>Admin UI</summary>
<img width="353" height="606" alt="bot" src="https://github.com/user-attachments/assets/882887a1-50ba-40a4-89d7-867532d9a416" />
</details>

<details>
   <summary>Trusted User UI</summary>
<img width="236" height="186" alt="Screenshot 2026-05-10 222408" src="https://github.com/user-attachments/assets/e4bf1dd9-902e-4820-a082-7e687eac0a21" />
</details>

## Files

| File | Purpose |
|---|---|
| `bot.py` | The bot itself |
| `requirements.txt` | Python dependencies |
| `install.sh` | Installer (creates venv, copies files, registers systemd unit) |
| `.env.example` | Template for configuration |

## Installation

1. Drop all four files into a directory on the server.
2. Run:
   ```
   sudo ./install.sh
   ```
3. Edit `/opt/windrose-bot/.env` — at minimum set `BOT_TOKEN` and `ADMIN_ID`.
4. Start:
   ```
   sudo systemctl enable --now windrose-bot
   ```
5. Watch:
   ```
   sudo journalctl -u windrose-bot -f
   ```

## Configuration reference

See `.env.example`. Required: `BOT_TOKEN`, `ADMIN_ID`. Other variables are optional with sensible defaults.

## Player-event detection

The bot tails `docker compose logs -f` and matches three log signatures:

| Event | Source line |
|---|---|
| Join (account ID) | `R5LogDataKeeper: ... Account connected. AccountId <ID>` |
| Join (player name) | `LogNet: Join succeeded: <Name>` |
| Leave | `R5LogCoopProxy: ... OnAccountDisconnected ... AccountId <ID>` |

The two join signals arrive ~1 second apart in either order. The bot keeps a 5-second pairing window with a half-buffer (whichever arrives first waits for its complement). Every event also has a 5-second dedup window because the server logs each one twice through different code paths. On startup, the bot seeds its active-player state from the most recent `Connected Accounts` table dump, so a bot restart while players are online does not produce phantom join notifications.

## Diagnosing parser issues

If notifications are not coming, set `LOG_WATCHER_DEBUG=true` in `.env`, restart the service, and watch `journalctl -u windrose-bot -f`. You will see:

- `WATCHER_RAW: ...` for the first 20 lines after watcher start (so you can see exactly what `docker compose logs` is delivering)
- `Account connected: <ID>`, `Join succeeded: <Name>`, `Player JOIN/LEAVE: ...` for every detected event

Turn it back off afterwards — it is verbose.

## Troubleshooting

- **Bot does not start, `Self-test failed` in logs:** the log format on your server differs from the fixtures. Capture the relevant line with `docker logs windrose | grep <pattern>` and adjust the regex; do not disable the self-test.
- **Bot does not respond to commands:** `sudo systemctl status windrose-bot`, then `sudo journalctl -u windrose-bot --since '5 min ago'`.
- **Permission errors on `/var/log/windrose-bot.log`:** `install.sh` creates the file with mode 644; the bot also logs to journald, so file logging is optional.
