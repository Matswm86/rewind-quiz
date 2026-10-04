# Rewind: the decades quiz

A multiplayer quiz for phones at **https://quiz.mwmai.no**. The questions cover history, culture, film, sport, celebrities, events and fun facts from the 1950s to the 2000s.

- **Two lengths**: 25 questions (about 10 minutes) or 50 questions (about 20). Questions run in decade order, spread evenly over the six decades, with categories varied inside each decade.
- **Up to 20 players**. The host creates a game and gets a 4-letter code, a QR code and a share link (the phone's share sheet on iOS and Android). Players join from their own phones.
- **Live rounds**: everyone sees the same question at the same moment with a 20-second timer. A correct answer scores 500 to 1,000 points depending on speed. A question closes early once every connected player has answered.
- **Scoreboard** after every question (top 5 plus your own place, with position changes), a **podium and full standings** at the end, a per-decade breakdown for each player, and an all-time **Hall of Fame** (top 20 per game length).
- **Rematch** keeps the group together and picks questions the room has not had yet.
- **Touch-first**: 64 px answer buttons, no double-tap zoom, haptic buzz on Android, the screen stays awake during a game, safe-area aware on notched iPhones. A reload or a locked phone drops you straight back into the game. It can be added to the home screen.
- The question bank has 110 questions (17 to 19 per decade), including Norwegian ones: Oslo 1952, Ekofisk, Grete Waitz, a-ha, Bobbysocks!, Gro Harlem Brundtland, Lillehammer, the 1994 EU vote, Bjørndalen and Rybak.

## Layout

```
rewind-quiz/
├── server/
│   ├── server.py          game server, Python standard library only
│   ├── questions.json     question bank (first answer in "a" is the correct one)
│   └── test_server.py     22 game-logic and HTTP tests
├── site/                  static client served by Caddy (no build step, no CDN)
│   ├── index.html, app.js, styles.css
│   ├── qrcode.js          qrcode-generator 1.4.4 (MIT, Kazuhiko Arase)
│   └── fonts/             Bungee + Atkinson Hyperlegible, self-hosted (OFL)
├── deploy/
│   ├── quiz.caddy         Caddy site block
│   ├── mwm-quiz.service   systemd user unit
│   └── install_remote.sh  runs on the VPS
├── deploy.sh              one-command deploy from the workstation
└── tests/
    ├── e2e.js             3 emulated phones play a full game (Playwright)
    └── load_test.py       20 players play a 50-question game over HTTP
```

## How it runs

Caddy serves `site/` from `/var/www/quiz` and proxies `/api/*` to `server.py` on `127.0.0.1:8795`, which runs as the `mwm-quiz` systemd user service. Game rooms live in memory, so restarting the service ends running games. The Hall of Fame is saved to `~/services/mwm-quiz/data/leaderboard.json`.

Clients long-poll `GET /api/state?g=CODE&p=TOKEN&v=VERSION`. The server holds the request until the room changes (or 25 s pass), so every phone gets updates within milliseconds without WebSockets. The server owns the clock and the answers: a question's correct option is only sent once it is revealed.

Every API reply is HTTP 200, errors included (`{"ok": false, "error": "no_game"}`). A room of 20 players on one home Wi-Fi shares a single public IP, so a fail2ban jail that bans an IP after a burst of 4xx responses would lock out the whole room. If you run fail2ban on the Caddy log, size any flood jail for that case before pointing it at this site.

## Deploy

From your workstation, in the repo root, with SSH access to a server that has Caddy, systemd and a user with sudo:

```bash
VPS=user@host ./deploy.sh
```

It runs the tests, uploads everything to `~/services/mwm-quiz/`, restarts the service, copies the site to `/var/www/quiz`, and appends `deploy/quiz.caddy` to `/etc/caddy/Caddyfile` the first time. It backs up the Caddyfile first, runs `caddy validate`, and restores the backup if validation fails. Later runs leave the Caddy block alone; edit it on the VPS if it ever needs to change. DNS already resolves `quiz.mwmai.no` to the VPS, so Caddy fetches the certificate on the first request.

Operations on the VPS:

```bash
systemctl --user status mwm-quiz
journalctl --user -u mwm-quiz -f
curl -s http://127.0.0.1:8795/api/health
```

To reset the Hall of Fame, stop the service, delete `~/services/mwm-quiz/data/leaderboard.json` and start it again.

## Local development

```bash
QUIZ_STATIC=site QUIZ_PORT=8799 python3 server/server.py     # open http://127.0.0.1:8799
python3 -m unittest discover -s server                          # unit tests
NODE_PATH=$(npm root -g) node tests/e2e.js http://127.0.0.1:8799 /tmp/shots
QUIZ_PORT=8798 QUIZ_QUESTION_MS=4000 QUIZ_REVEAL_MS=500 python3 server/server.py &
python3 tests/load_test.py http://127.0.0.1:8798
```

`QUIZ_QUESTION_MS` and `QUIZ_REVEAL_MS` change the timer (default 20 s) and the pause after each reveal (default 8 s).

## Adding questions

Add entries to `server/questions.json` with a unique `id`, a `decade` (1950 to 2000), one of the seven categories (`History`, `Events`, `Culture`, `Film`, `Celebrities`, `Sports`, `Fun Facts`), four answers with the correct one first, and a one- or two-sentence `fact` shown after the reveal. Each decade needs at least 9 questions for a 50-question game. Restart the service to load changes.

## License

MIT, see [LICENSE](LICENSE). The bundled QR code library (qrcode-generator, MIT) and the fonts (Bungee and Atkinson Hyperlegible, SIL OFL, see `site/fonts/OFL.txt`) keep their own licenses.
