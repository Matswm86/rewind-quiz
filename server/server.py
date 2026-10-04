#!/usr/bin/env python3
"""Rewind: multiplayer decades quiz server behind quiz.mwmai.no.

Standard library only. Caddy serves the static site from /var/www/quiz and
proxies /api/* to this process. Rooms live in memory; the all-time leaderboard
is persisted to $QUIZ_DATA_DIR/leaderboard.json.

Game flow: the host creates a room (25 or 50 questions) and gets a 4-letter
code. Up to 20 players join. The host starts, then every player gets the same
question at the same moment. A question closes when its timer runs out or when
every online player has answered, the answer is revealed with the scoreboard,
and the next question follows automatically.

Every API reply is HTTP 200, with {"ok": false, "error": ...} on failure.
Twenty players on one home Wi-Fi share a single public IP, so a fail2ban jail
that bans on bursts of 4xx responses would lock out the whole room.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import random
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent

HOST = os.environ.get("QUIZ_HOST", "127.0.0.1")
PORT = int(os.environ.get("QUIZ_PORT", "8795"))
QUESTIONS_PATH = Path(os.environ.get("QUIZ_QUESTIONS", HERE / "questions.json"))
DATA_DIR = Path(os.environ.get("QUIZ_DATA_DIR", HERE / "data"))
STATIC_DIR = os.environ.get("QUIZ_STATIC")  # local dev only: serve the site as well

QUESTION_MS = int(os.environ.get("QUIZ_QUESTION_MS", "20000"))
REVEAL_MS = int(os.environ.get("QUIZ_REVEAL_MS", "8000"))
MAX_PLAYERS = 20
MODES = (25, 50)
DECADES = (1950, 1960, 1970, 1980, 1990, 2000)
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # no I or O, they read as 1 and 0
NAME_MAX = 16
POLL_HOLD_S = 25.0
ONLINE_GRACE_S = 12.0  # a player counts as online this long after their last poll
HOST_HANDOVER_S = 45.0  # lobby/final: pass the host role on when the host goes quiet
ROOM_IDLE_S = 3 * 3600
MAX_ROOMS = 300
CREATES_PER_IP_HOUR = 30
LEADERBOARD_KEEP = 100
LEADERBOARD_SHOW = 20
BODY_MAX = 2048
TICK_S = 0.2

log = logging.getLogger("rewind")

clock = time.monotonic  # tests swap this for a fake clock
rng = random.Random()

COND = threading.Condition()
ROOMS: dict[str, Room] = {}
CREATES: dict[str, list[float]] = {}
BANK: list[dict] = []
LEADERBOARD: dict[str, list[dict]] = {str(m): [] for m in MODES}


@dataclass
class Player:
    id: str
    token: str
    name: str
    joined: float
    score: int = 0
    correct: int = 0
    streak: int = 0
    answers: dict = field(default_factory=dict)  # question index -> (choice, points, ms)
    rank: int = 1
    prev_rank: int = 1
    gained: int = 0  # points from the most recently revealed question
    last_seen: float = 0.0
    polls: int = 0  # open long-polls
    online: bool = True  # last online state broadcast to the room
    left: bool = False

    def reset(self) -> None:
        self.score = self.correct = self.streak = self.gained = 0
        self.answers = {}
        self.rank = self.prev_rank = 1


@dataclass
class Room:
    code: str
    mode: int
    host_id: str
    created: float
    players: dict = field(default_factory=dict)  # id -> Player, in join order
    phase: str = "lobby"  # lobby | question | reveal | finished
    questions: list = field(default_factory=list)
    index: int = -1
    q_started: float = 0.0
    deadline: float = 0.0
    version: int = 1
    game_no: int = 0
    used: set = field(default_factory=set)
    hof: dict = field(default_factory=dict)  # player id -> hall of fame rank
    last_activity: float = 0.0

    def by_token(self, token: str) -> Player | None:
        if not token:
            return None
        for p in self.players.values():
            if secrets.compare_digest(p.token, token):
                return p
        return None

    def active(self) -> list[Player]:
        return [p for p in self.players.values() if not p.left]


class ApiError(Exception):
    """A user-facing failure, returned as {"ok": false, "error": code}."""


# --------------------------------------------------------------------------- helpers
def now() -> float:
    return clock()


def bump(room: Room) -> None:
    room.version += 1
    room.last_activity = now()
    COND.notify_all()


def is_online(p: Player, t: float) -> bool:
    return not p.left and (p.polls > 0 or t - p.last_seen < ONLINE_GRACE_S)


def clean_name(raw) -> str:
    if not isinstance(raw, str):
        return ""
    name = "".join(ch for ch in raw if ch.isprintable())
    return re.sub(r"\s+", " ", name).strip()[:NAME_MAX].strip()


def clean_code(raw) -> str:
    return re.sub(r"[^A-Z]", "", str(raw or "").upper())[:4]


def unique_name(room: Room, name: str) -> str:
    taken = {p.name.casefold() for p in room.players.values()}
    if name.casefold() not in taken:
        return name
    for n in range(2, 100):
        candidate = f"{name[: NAME_MAX - 3].rstrip()} {n}"
        if candidate.casefold() not in taken:
            return candidate
    raise ApiError("name_taken")


def new_player(room: Room, name: str) -> Player:
    t = now()
    p = Player(
        id=secrets.token_hex(4),
        token=secrets.token_urlsafe(18),
        name=unique_name(room, name),
        joined=t,
        last_seen=t,
    )
    room.players[p.id] = p
    return p


def rank_players(room: Room) -> None:
    ordered = sorted(room.players.values(), key=lambda p: (-p.score, p.joined))
    rank, last_score = 0, None
    for pos, p in enumerate(ordered, start=1):
        if p.score != last_score:
            rank, last_score = pos, p.score
        p.rank = rank


def pick_questions(mode: int, avoid: set) -> list[dict]:
    """Pick `mode` questions spread evenly over the decades, in decade order.

    Within a decade, unused questions come first and categories are varied
    before any category repeats. Answer options are shuffled per game.
    """
    per = {d: mode // len(DECADES) for d in DECADES}
    for d in rng.sample(DECADES, mode % len(DECADES)):
        per[d] += 1

    chosen: list[dict] = []
    for d in DECADES:
        pool = [q for q in BANK if q["decade"] == d]
        rng.shuffle(pool)
        fresh = [q for q in pool if q["id"] not in avoid]
        seen = [q for q in pool if q["id"] in avoid]
        picked: list[dict] = []
        for group in (fresh, seen):
            cats = {q["cat"] for q in picked}
            for q in group:  # first pass: one per category
                if len(picked) < per[d] and q["cat"] not in cats:
                    picked.append(q)
                    cats.add(q["cat"])
            for q in group:  # second pass: fill up
                if len(picked) < per[d] and q not in picked:
                    picked.append(q)
        rng.shuffle(picked)
        chosen.extend(picked)

    built = []
    for q in chosen:
        order = rng.sample(range(4), 4)
        built.append(
            {
                "id": q["id"],
                "decade": q["decade"],
                "cat": q["cat"],
                "q": q["q"],
                "options": [q["a"][i] for i in order],
                "correct": order.index(0),
                "fact": q["fact"],
            }
        )
    return built


# --------------------------------------------------------------------------- game flow
# All functions below expect COND to be held.
def begin_question(room: Room, index: int) -> None:
    t = now()
    room.phase = "question"
    room.index = index
    room.q_started = t
    room.deadline = t + QUESTION_MS / 1000
    bump(room)


def all_answered(room: Room) -> bool:
    t = now()
    online = [p for p in room.players.values() if is_online(p, t)]
    return bool(online) and all(room.index in p.answers for p in online)


def end_question(room: Room) -> None:
    q = room.questions[room.index]
    rank_players(room)
    for p in room.players.values():
        p.prev_rank = p.rank
        ans = p.answers.get(room.index)
        p.gained = ans[1] if ans else 0
        p.score += p.gained
        if ans and ans[0] == q["correct"]:
            p.correct += 1
            p.streak += 1
        else:
            p.streak = 0
    rank_players(room)
    room.phase = "reveal"
    room.deadline = now() + REVEAL_MS / 1000
    bump(room)


def advance(room: Room) -> None:
    if room.index + 1 < len(room.questions):
        begin_question(room, room.index + 1)
    else:
        finish(room)


def finish(room: Room) -> None:
    room.phase = "finished"
    room.deadline = 0.0
    record_leaderboard(room)
    bump(room)


def handover_host(room: Room) -> None:
    t = now()
    candidates = [p for p in room.players.values() if is_online(p, t)] or room.active()
    if candidates:
        room.host_id = candidates[0].id


def tick() -> None:
    """Advance timers, track who is online, hand over host, expire rooms."""
    with COND:
        t = now()
        for code, room in list(ROOMS.items()):
            if t - room.last_activity > ROOM_IDLE_S:
                del ROOMS[code]
                log.info("room %s expired", code)
                continue
            changed = False
            for p in room.players.values():
                online = is_online(p, t)
                if online != p.online:
                    p.online = online
                    changed = True
            host = room.players.get(room.host_id)
            if (
                room.phase in ("lobby", "finished")
                and host is not None
                and not is_online(host, t)
                and t - host.last_seen > HOST_HANDOVER_S
            ):
                old = room.host_id
                handover_host(room)
                changed = changed or room.host_id != old
            if room.phase == "question" and (t >= room.deadline or all_answered(room)):
                end_question(room)
            elif room.phase == "reveal" and t >= room.deadline:
                advance(room)
            elif changed:
                bump(room)
        cutoff = t - 3600
        for ip in list(CREATES):
            CREATES[ip] = [x for x in CREATES[ip] if x > cutoff]
            if not CREATES[ip]:
                del CREATES[ip]


def ticker() -> None:
    while True:
        try:
            tick()
        except Exception:  # keep the game loop alive whatever happens
            log.exception("tick failed")
        time.sleep(TICK_S)


# --------------------------------------------------------------------------- leaderboard
def load_leaderboard() -> None:
    path = DATA_DIR / "leaderboard.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        log.exception("could not read %s, starting empty", path)
        return
    for m in LEADERBOARD:
        rows = data.get(m)
        if isinstance(rows, list):
            LEADERBOARD[m] = rows[:LEADERBOARD_KEEP]


def save_leaderboard() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / "leaderboard.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(LEADERBOARD, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def record_leaderboard(room: Room) -> None:
    board = LEADERBOARD[str(room.mode)]
    date = datetime.now(UTC).date().isoformat()
    field_size = len(room.active())
    entries = {}
    for p in room.players.values():
        if not p.answers:
            continue
        entry = {
            "id": secrets.token_hex(6),
            "name": p.name,
            "score": p.score,
            "correct": p.correct,
            "total": len(room.questions),
            "players": field_size,
            "date": date,
        }
        board.append(entry)
        entries[p.id] = entry["id"]
    board.sort(key=lambda e: -e["score"])  # stable: earlier entries win ties
    del board[LEADERBOARD_KEEP:]
    positions = {e["id"]: i + 1 for i, e in enumerate(board[:LEADERBOARD_SHOW])}
    room.hof = {pid: positions[eid] for pid, eid in entries.items() if eid in positions}
    try:
        save_leaderboard()
    except OSError:
        log.exception("could not save leaderboard")


# --------------------------------------------------------------------------- snapshots
def snapshot(room: Room, me: Player) -> dict:
    t = now()
    if room.phase == "lobby":
        ordered = list(room.players.values())
    else:
        ordered = sorted(room.players.values(), key=lambda p: (p.rank, p.joined))
    players = [
        {
            "id": p.id,
            "name": p.name,
            "score": p.score,
            "correct": p.correct,
            "rank": p.rank,
            "prev_rank": p.prev_rank,
            "gained": p.gained,
            "streak": p.streak,
            "host": p.id == room.host_id,
            "online": is_online(p, t),
            "left": p.left,
            "answered": room.phase == "question" and room.index in p.answers,
        }
        for p in ordered
        if not (p.left and room.phase == "lobby")
    ]
    snap = {
        "ok": True,
        "v": room.version,
        "code": room.code,
        "mode": room.mode,
        "phase": room.phase,
        "game": room.game_no,
        "max_players": MAX_PLAYERS,
        "you": {
            "id": me.id,
            "name": me.name,
            "host": me.id == room.host_id,
            "score": me.score,
            "correct": me.correct,
            "rank": me.rank,
            "streak": me.streak,
            "gained": me.gained,
        },
        "players": players,
    }
    if room.phase in ("question", "reveal"):
        q = room.questions[room.index]
        mine = me.answers.get(room.index)
        online = [p for p in room.players.values() if is_online(p, t)]
        snap["q"] = {
            "index": room.index,
            "total": len(room.questions),
            "plan": [sum(1 for x in room.questions if x["decade"] == d) for d in DECADES],
            "decade": q["decade"],
            "cat": q["cat"],
            "text": q["q"],
            "options": q["options"],
            "duration_ms": QUESTION_MS,
            "remaining_ms": max(0, int((room.deadline - t) * 1000))
            if room.phase == "question"
            else 0,
            "your_choice": mine[0] if mine else None,
            "answered": sum(1 for p in room.players.values() if room.index in p.answers),
            "online": len(online),
        }
        if room.phase == "reveal":
            counts = [0, 0, 0, 0]
            for p in room.players.values():
                ans = p.answers.get(room.index)
                if ans:
                    counts[ans[0]] += 1
            snap["reveal"] = {
                "correct": q["correct"],
                "fact": q["fact"],
                "counts": counts,
                "your_points": mine[1] if mine else 0,
                "your_ms": mine[2] if mine else None,
                "next_ms": max(0, int((room.deadline - t) * 1000)),
                "last": room.index + 1 == len(room.questions),
            }
    if room.phase == "finished":
        by_decade = []
        for d in DECADES:
            asked = [i for i, q in enumerate(room.questions) if q["decade"] == d]
            right = sum(
                1
                for i in asked
                if i in me.answers and me.answers[i][0] == room.questions[i]["correct"]
            )
            by_decade.append({"decade": d, "right": right, "asked": len(asked)})
        snap["final"] = {
            "total": len(room.questions),
            "by_decade": by_decade,
            "hof_rank": room.hof.get(me.id),
        }
    return snap


# --------------------------------------------------------------------------- API
def _room(code) -> Room:
    room = ROOMS.get(clean_code(code))
    if room is None:
        raise ApiError("no_game")
    return room


def _member(room: Room, token) -> Player:
    p = room.by_token(token if isinstance(token, str) else "")
    if p is None:
        raise ApiError("not_in_game")
    return p


def _require_host(room: Room, p: Player) -> None:
    if p.id != room.host_id:
        raise ApiError("host_only")


def _joined(room: Room, p: Player) -> dict:
    return {"ok": True, "code": room.code, "token": p.token, "id": p.id, "name": p.name}


def api_create(data: dict, ip: str) -> dict:
    name = clean_name(data.get("name"))
    if not name:
        raise ApiError("name_required")
    try:
        mode = int(data.get("mode", 25))
    except (TypeError, ValueError):
        mode = 0
    if mode not in MODES:
        raise ApiError("bad_mode")
    with COND:
        t = now()
        recent = [x for x in CREATES.get(ip, []) if x > t - 3600]
        if len(recent) >= CREATES_PER_IP_HOUR:
            raise ApiError("slow_down")
        if len(ROOMS) >= MAX_ROOMS:
            raise ApiError("server_busy")
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
        while code in ROOMS:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
        CREATES[ip] = recent + [t]
        room = Room(code=code, mode=mode, host_id="", created=t, last_activity=t)
        host = new_player(room, name)
        room.host_id = host.id
        ROOMS[code] = room
        log.info("room %s created (%d questions)", code, mode)
        return _joined(room, host)


def api_join(data: dict, ip: str) -> dict:
    with COND:
        room = _room(data.get("code"))
        back = room.by_token(data.get("token") if isinstance(data.get("token"), str) else "")
        if back is not None:
            if back.left:
                if len(room.active()) >= MAX_PLAYERS:
                    raise ApiError("game_full")
                back.left = False
            back.last_seen = now()
            bump(room)
            return _joined(room, back)
        name = clean_name(data.get("name"))
        if not name:
            raise ApiError("name_required")
        if room.phase == "finished":
            raise ApiError("game_over")
        if len(room.active()) >= MAX_PLAYERS:
            raise ApiError("game_full")
        p = new_player(room, name)
        if room.phase != "lobby":
            rank_players(room)
            p.prev_rank = p.rank
        bump(room)
        return _joined(room, p)


def api_start(data: dict, ip: str) -> dict:
    with COND:
        room = _room(data.get("code"))
        p = _member(room, data.get("token"))
        _require_host(room, p)
        if room.phase != "lobby":
            raise ApiError("already_started")
        room.questions = pick_questions(room.mode, room.used)
        room.used |= {q["id"] for q in room.questions}
        if len(room.used) > len(BANK) - room.mode:
            room.used = {q["id"] for q in room.questions}
        room.game_no += 1
        room.hof = {}
        for pl in room.players.values():
            pl.reset()
        log.info(
            "room %s game %d started with %d players", room.code, room.game_no, len(room.active())
        )
        begin_question(room, 0)
        return {"ok": True}


def api_answer(data: dict, ip: str) -> dict:
    with COND:
        room = _room(data.get("code"))
        p = _member(room, data.get("token"))
        try:
            index = int(data.get("index"))
            choice = int(data.get("choice"))
        except (TypeError, ValueError):
            raise ApiError("bad_answer") from None
        if room.phase != "question" or index != room.index:
            raise ApiError("too_late")
        if index in p.answers:
            raise ApiError("already_answered")
        if not 0 <= choice < 4:
            raise ApiError("bad_answer")
        t = now()
        remaining = max(0.0, room.deadline - t)
        q = room.questions[index]
        points = (
            500 + round(500 * remaining / (QUESTION_MS / 1000)) if choice == q["correct"] else 0
        )
        p.answers[index] = (choice, points, int((t - room.q_started) * 1000))
        p.last_seen = t
        if all_answered(room):
            end_question(room)
        else:
            bump(room)
        return {"ok": True}


def api_next(data: dict, ip: str) -> dict:
    with COND:
        room = _room(data.get("code"))
        p = _member(room, data.get("token"))
        _require_host(room, p)
        if room.phase != "reveal":
            raise ApiError("not_now")
        advance(room)
        return {"ok": True}


def api_again(data: dict, ip: str) -> dict:
    with COND:
        room = _room(data.get("code"))
        p = _member(room, data.get("token"))
        _require_host(room, p)
        if room.phase != "finished":
            raise ApiError("not_now")
        room.players = {pid: pl for pid, pl in room.players.items() if not pl.left}
        for pl in room.players.values():
            pl.reset()
        room.phase = "lobby"
        room.index = -1
        room.questions = []
        room.hof = {}
        bump(room)
        return {"ok": True}


def api_leave(data: dict, ip: str) -> dict:
    with COND:
        room = _room(data.get("code"))
        p = _member(room, data.get("token"))
        if room.phase == "lobby":
            del room.players[p.id]
        else:
            p.left = True
        if not room.active():
            del ROOMS[room.code]
            COND.notify_all()
            return {"ok": True}
        if p.id == room.host_id:
            handover_host(room)
        if room.phase == "question" and all_answered(room):
            end_question(room)
        else:
            bump(room)
        return {"ok": True}


def api_state(code, token, since: int, hold: float = POLL_HOLD_S) -> dict:
    with COND:
        room = _room(code)
        p = _member(room, token)
        t = now()
        p.last_seen = t
        room.last_activity = t
        if not p.online and not p.left:
            p.online = True
            bump(room)
        p.polls += 1
        try:
            end = t + hold
            while room.version <= since and ROOMS.get(room.code) is room and p.id in room.players:
                remaining = end - now()
                if remaining <= 0:
                    break
                COND.wait(min(remaining, 1.0))
        finally:
            p.polls -= 1
            p.last_seen = now()
        if ROOMS.get(room.code) is not room:
            raise ApiError("no_game")
        if p.id not in room.players:
            raise ApiError("not_in_game")
        return snapshot(room, p)


def api_leaderboard() -> dict:
    with COND:
        return {
            "ok": True,
            "boards": {m: rows[:LEADERBOARD_SHOW] for m, rows in LEADERBOARD.items()},
        }


POST_ACTIONS = {
    "/api/create": api_create,
    "/api/join": api_join,
    "/api/start": api_start,
    "/api/answer": api_answer,
    "/api/next": api_next,
    "/api/again": api_again,
    "/api/leave": api_leave,
}


# --------------------------------------------------------------------------- HTTP
class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Rewind"
    sys_version = ""

    def log_message(self, fmt, *args) -> None:  # Caddy keeps the access log
        pass

    def client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For", "")
        return forwarded.split(",")[-1].strip() or self.client_address[0]

    def send_body(
        self, body: bytes, ctype: str, status: int = 200, cache: str = "no-store"
    ) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", cache)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def send_json(self, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_body(body, "application/json; charset=utf-8")

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            if STATIC_DIR:
                self.serve_static(url.path)
            else:
                self.send_json({"ok": False, "error": "not_found"})
            return
        qs = parse_qs(url.query)
        arg = lambda k: (qs.get(k) or [""])[0]  # noqa: E731
        try:
            if url.path == "/api/state":
                try:
                    since = int(arg("v") or 0)
                except ValueError:
                    since = 0
                self.send_json(api_state(arg("g"), arg("p"), since))
            elif url.path == "/api/leaderboard":
                self.send_json(api_leaderboard())
            elif url.path == "/api/health":
                with COND:
                    health = {"ok": True, "rooms": len(ROOMS), "questions": len(BANK)}
                self.send_json(health)
            else:
                self.send_json({"ok": False, "error": "not_found"})
        except ApiError as e:
            self.send_json({"ok": False, "error": str(e)})
        except Exception:
            log.exception("GET %s failed", url.path)
            self.send_json({"ok": False, "error": "server_error"})

    def do_POST(self) -> None:
        url = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > BODY_MAX:
            self.close_connection = True
            self.send_json({"ok": False, "error": "bad_request"})
            return
        raw = self.rfile.read(length) if length else b""
        action = POST_ACTIONS.get(url.path)
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = None
        if action is None or not isinstance(data, dict):
            self.send_json({"ok": False, "error": "bad_request"})
            return
        try:
            self.send_json(action(data, self.client_ip()))
        except ApiError as e:
            self.send_json({"ok": False, "error": str(e)})
        except Exception:
            log.exception("POST %s failed", url.path)
            self.send_json({"ok": False, "error": "server_error"})

    def serve_static(self, path: str) -> None:
        root = Path(STATIC_DIR).resolve()
        target = (root / path.lstrip("/")).resolve()
        if target.is_dir():
            target = target / "index.html"
        if root not in target.parents or not target.is_file():
            self.send_body(b"not found", "text/plain", status=404)
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix == ".webmanifest":
            ctype = "application/manifest+json"
        self.send_body(target.read_bytes(), ctype, cache="no-cache")


def load_bank() -> None:
    BANK[:] = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    for d in DECADES:
        n = sum(1 for q in BANK if q["decade"] == d)
        if n < -(-max(MODES) // len(DECADES)):
            raise SystemExit(
                f"only {n} questions for the {d}s; need more for a {max(MODES)}-question game"
            )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_bank()
    load_leaderboard()
    threading.Thread(target=ticker, name="ticker", daemon=True).start()
    server = Server((HOST, PORT), Handler)
    log.info("Rewind listening on %s:%d with %d questions", HOST, PORT, len(BANK))
    server.serve_forever()


if __name__ == "__main__":
    main()
