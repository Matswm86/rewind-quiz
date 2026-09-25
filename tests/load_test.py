"""Twenty simulated players play a full 50-question game over HTTP with long-polling.

    QUIZ_PORT=8799 QUIZ_QUESTION_MS=4000 QUIZ_REVEAL_MS=500 python3 server/server.py &
    python3 tests/load_test.py http://127.0.0.1:8799

Each player runs its own long-poll loop, like a phone would, and answers after a
random delay. Checks the 21st player is turned away and that every player sees
the same final standings.
"""
from __future__ import annotations

import json
import random
import sys
import threading
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8799"


def call(path: str, body: dict | None = None, timeout: float = 40) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        assert r.status == 200
        return json.loads(r.read())


def player(me: dict, results: dict, errors: list) -> None:
    v = 0
    answered = set()
    try:
        while True:
            s = call(f"/api/state?g={me['code']}&p={me['token']}&v={v}")
            assert s["ok"], s
            v = s["v"]
            if s["phase"] == "question" and s["q"]["index"] not in answered:
                i = s["q"]["index"]
                answered.add(i)
                time.sleep(random.uniform(0.05, 1.5))
                r = call("/api/answer", {"code": me["code"], "token": me["token"], "index": i, "choice": random.randrange(4)})
                assert r["ok"] or r["error"] == "too_late", r
            if s["phase"] == "finished":
                results[me["id"]] = [(p["name"], p["score"]) for p in s["players"]]
                return
    except Exception as e:  # surface failures from worker threads
        errors.append(f"{me.get('name')}: {e!r}")


def main() -> None:
    host = call("/api/create", {"name": "Host", "mode": 50})
    players = [host] + [call("/api/join", {"code": host["code"], "name": f"Player {i}"}) for i in range(1, 20)]
    assert all(p["ok"] for p in players)
    extra = call("/api/join", {"code": host["code"], "name": "Number 21"})
    assert extra == {"ok": False, "error": "game_full"}, extra
    print(f"20 players in game {host['code']}; 21st turned away")

    results: dict = {}
    errors: list = []
    threads = [threading.Thread(target=player, args=(p, results, errors)) for p in players]
    for t in threads:
        t.start()
    time.sleep(0.5)
    assert call("/api/start", {"code": host["code"], "token": host["token"]})["ok"]
    started = time.time()
    for t in threads:
        t.join(timeout=600)
    assert not errors, errors
    assert len(results) == 20, f"only {len(results)} players finished"
    boards = {json.dumps(b) for b in results.values()}
    assert len(boards) == 1, "players saw different final standings"
    board = results[host["id"]]
    print(f"50 questions played in {time.time() - started:.0f}s; winner {board[0][0]} with {board[0][1]:,} points")
    print("LOAD TEST PASSED")


if __name__ == "__main__":
    main()
