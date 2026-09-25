"""Game-logic tests for server.py. Run: python3 -m unittest test_server -v"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

import server as S


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def state(res: dict) -> dict:
    return S.api_state(res["code"], res["token"], 0, hold=0)


class GameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        S.load_bank()

    def setUp(self) -> None:
        self.clock = FakeClock()
        S.clock = self.clock
        S.rng.seed(7)
        S.ROOMS.clear()
        S.CREATES.clear()
        self.tmp = tempfile.TemporaryDirectory()
        S.DATA_DIR = Path(self.tmp.name)
        for m in S.LEADERBOARD:
            S.LEADERBOARD[m] = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def host(self, mode: int = 25, name: str = "Mats") -> dict:
        return S.api_create({"name": name, "mode": mode}, "1.2.3.4")

    def join(self, code: str, name: str) -> dict:
        return S.api_join({"code": code, "name": name}, "1.2.3.4")

    def room(self, code: str) -> S.Room:
        return S.ROOMS[code]

    # ------------------------------------------------------------------ selection
    def test_pick_spreads_decades_in_order(self) -> None:
        for mode in S.MODES:
            qs = S.pick_questions(mode, set())
            self.assertEqual(len(qs), mode)
            self.assertEqual(len({q["id"] for q in qs}), mode)
            decades = [q["decade"] for q in qs]
            self.assertEqual(decades, sorted(decades), "questions should run 1950s to 2000s")
            counts = [decades.count(d) for d in S.DECADES]
            self.assertLessEqual(max(counts) - min(counts), 1)
            for q in qs:
                src = next(b for b in S.BANK if b["id"] == q["id"])
                self.assertEqual(q["options"][q["correct"]], src["a"][0])

    def test_rematch_prefers_new_questions(self) -> None:
        first = S.pick_questions(25, set())
        second = S.pick_questions(25, {q["id"] for q in first})
        self.assertFalse({q["id"] for q in first} & {q["id"] for q in second})

    # ------------------------------------------------------------------ lobby
    def test_create_join_and_unique_names(self) -> None:
        h = self.host()
        self.assertEqual(len(h["code"]), 4)
        a = self.join(h["code"], "Anna")
        b = self.join(h["code"].lower(), "anna")
        self.assertEqual(a["name"], "Anna")
        self.assertEqual(b["name"], "anna 2")
        snap = state(h)
        self.assertEqual(snap["phase"], "lobby")
        self.assertEqual([p["name"] for p in snap["players"]], ["Mats", "Anna", "anna 2"])
        self.assertTrue(snap["you"]["host"])

    def test_bad_inputs_are_rejected(self) -> None:
        with self.assertRaises(S.ApiError):
            S.api_create({"name": "   ", "mode": 25}, "ip")
        with self.assertRaises(S.ApiError):
            S.api_create({"name": "X", "mode": 30}, "ip")
        with self.assertRaises(S.ApiError):
            self.join("ZZZZ", "Nobody")

    def test_twenty_player_cap(self) -> None:
        h = self.host()
        for i in range(S.MAX_PLAYERS - 1):
            self.join(h["code"], f"P{i}")
        with self.assertRaisesRegex(S.ApiError, "game_full"):
            self.join(h["code"], "One too many")

    def test_rejoin_with_token_keeps_player(self) -> None:
        h = self.host()
        a = self.join(h["code"], "Anna")
        again = S.api_join({"code": h["code"], "token": a["token"]}, "ip")
        self.assertEqual(again["id"], a["id"])
        self.assertEqual(len(self.room(h["code"]).players), 2)

    def test_only_host_can_start(self) -> None:
        h = self.host()
        a = self.join(h["code"], "Anna")
        with self.assertRaisesRegex(S.ApiError, "host_only"):
            S.api_start({"code": h["code"], "token": a["token"]}, "ip")

    def test_create_rate_limit(self) -> None:
        for _ in range(S.CREATES_PER_IP_HOUR):
            S.api_create({"name": "Spam", "mode": 25}, "9.9.9.9")
        with self.assertRaisesRegex(S.ApiError, "slow_down"):
            S.api_create({"name": "Spam", "mode": 25}, "9.9.9.9")
        S.api_create({"name": "Other", "mode": 25}, "8.8.8.8")

    # ------------------------------------------------------------------ play
    def start(self, h: dict) -> S.Room:
        S.api_start({"code": h["code"], "token": h["token"]}, "ip")
        return self.room(h["code"])

    def answer(self, res: dict, index: int, choice: int) -> dict:
        return S.api_answer({"code": res["code"], "token": res["token"], "index": index, "choice": choice}, "ip")

    def test_question_hides_answer_until_reveal(self) -> None:
        h = self.host()
        self.join(h["code"], "Anna")
        self.start(h)
        snap = state(h)
        self.assertEqual(snap["phase"], "question")
        self.assertNotIn("reveal", snap)
        self.assertNotIn("correct", json.dumps(snap["q"]))
        self.assertEqual(snap["q"]["total"], 25)

    def test_speed_scoring_and_early_close(self) -> None:
        h = self.host()
        a = self.join(h["code"], "Anna")
        room = self.start(h)
        right = room.questions[0]["correct"]
        wrong = (right + 1) % 4
        self.answer(h, 0, right)  # instant answer: full 1000
        self.assertEqual(room.phase, "question", "waits for Anna")
        self.clock.advance(10)
        self.answer(a, 0, wrong)
        self.assertEqual(room.phase, "reveal", "everyone answered, so it closes early")
        snap = state(h)
        self.assertEqual(snap["reveal"]["correct"], right)
        self.assertEqual(snap["you"]["score"], 1000)
        self.assertEqual(snap["players"][0]["name"], "Mats")
        self.assertEqual(state(a)["you"]["score"], 0)
        self.assertEqual(sum(snap["reveal"]["counts"]), 2)

    def test_half_time_answer_scores_750(self) -> None:
        h = self.host()
        room = self.start(h)
        self.join(h["code"], "Anna")  # joins mid-question, never answers
        self.clock.advance(S.QUESTION_MS / 2000)
        self.answer(h, 0, room.questions[0]["correct"])
        self.clock.advance(S.QUESTION_MS / 1000)
        S.tick()
        self.assertEqual(room.phase, "reveal")
        self.assertEqual(state(h)["you"]["score"], 750)

    def test_scores_hidden_during_question(self) -> None:
        h = self.host()
        a = self.join(h["code"], "Anna")
        room = self.start(h)
        self.answer(h, 0, room.questions[0]["correct"])
        snap = state(a)
        self.assertEqual({p["score"] for p in snap["players"]}, {0})
        self.assertTrue(next(p for p in snap["players"] if p["name"] == "Mats")["answered"])

    def test_answer_rules(self) -> None:
        h = self.host()
        a = self.join(h["code"], "Anna")
        room = self.start(h)
        self.answer(h, 0, 0)
        with self.assertRaisesRegex(S.ApiError, "already_answered"):
            self.answer(h, 0, 1)
        with self.assertRaisesRegex(S.ApiError, "too_late"):
            self.answer(a, 1, 0)
        with self.assertRaisesRegex(S.ApiError, "bad_answer"):
            self.answer(a, 0, 7)
        self.assertEqual(room.phase, "question")

    def test_timer_then_auto_advance(self) -> None:
        h = self.host()
        room = self.start(h)
        self.join(h["code"], "Anna")
        self.clock.advance(S.QUESTION_MS / 1000 + 0.1)
        S.tick()
        self.assertEqual(room.phase, "reveal")
        self.clock.advance(S.REVEAL_MS / 1000 + 0.1)
        S.tick()
        self.assertEqual((room.phase, room.index), ("question", 1))

    def test_offline_player_does_not_stall_question(self) -> None:
        h = self.host()
        self.join(h["code"], "Anna")
        room = self.start(h)
        self.clock.advance(S.ONLINE_GRACE_S + 1)  # Anna never polls again
        S.api_state(h["code"], h["token"], 0, hold=0)  # host is still here
        self.answer(h, 0, 0)
        self.assertEqual(room.phase, "reveal")

    def test_host_next_skips_reveal(self) -> None:
        h = self.host()
        room = self.start(h)
        self.answer(h, 0, 0)
        S.api_next({"code": h["code"], "token": h["token"]}, "ip")
        self.assertEqual((room.phase, room.index), ("question", 1))

    def play_through(self, h: dict, others: list[dict]) -> S.Room:
        room = self.start(h)
        for i, q in enumerate(room.questions):
            self.answer(h, i, q["correct"])
            for o in others:
                self.answer(o, i, (q["correct"] + 1) % 4)
            S.api_next({"code": h["code"], "token": h["token"]}, "ip")
        return room

    def test_full_game_leaderboard_and_rematch(self) -> None:
        h = self.host()
        a = self.join(h["code"], "Anna")
        room = self.play_through(h, [a])
        self.assertEqual(room.phase, "finished")
        snap = state(h)
        self.assertEqual(snap["you"]["correct"], 25)
        self.assertEqual(snap["you"]["score"], 25_000)
        self.assertEqual(snap["final"]["hof_rank"], 1)
        self.assertEqual(sum(d["right"] for d in snap["final"]["by_decade"]), 25)
        board = S.api_leaderboard()["boards"]["25"]
        self.assertEqual([e["name"] for e in board], ["Mats", "Anna"])
        saved = json.loads((S.DATA_DIR / "leaderboard.json").read_text())
        self.assertEqual(saved["25"][0]["score"], 25_000)

        first_ids = {q["id"] for q in room.questions}
        with self.assertRaisesRegex(S.ApiError, "game_over"):
            self.join(h["code"], "Late")
        S.api_again({"code": h["code"], "token": h["token"]}, "ip")
        self.assertEqual(state(a)["phase"], "lobby")
        self.assertEqual(state(a)["you"]["score"], 0)
        self.start(h)
        self.assertFalse(first_ids & {q["id"] for q in room.questions})

    def test_leave_hands_over_host(self) -> None:
        h = self.host()
        a = self.join(h["code"], "Anna")
        S.api_leave({"code": h["code"], "token": h["token"]}, "ip")
        snap = state(a)
        self.assertTrue(snap["you"]["host"])
        self.assertEqual([p["name"] for p in snap["players"]], ["Anna"])
        S.api_leave({"code": a["code"], "token": a["token"]}, "ip")
        self.assertNotIn(h["code"], S.ROOMS)

    def test_quiet_host_hands_over_in_lobby(self) -> None:
        h = self.host()
        a = self.join(h["code"], "Anna")
        self.clock.advance(S.HOST_HANDOVER_S + 1)
        S.api_state(a["code"], a["token"], 0, hold=0)
        S.tick()
        self.assertTrue(state(a)["you"]["host"])

    def test_idle_rooms_expire(self) -> None:
        h = self.host()
        self.clock.advance(S.ROOM_IDLE_S + 1)
        S.tick()
        self.assertNotIn(h["code"], S.ROOMS)


class HttpTests(unittest.TestCase):
    """One real round trip through the HTTP layer, including a long-poll wake-up."""

    @classmethod
    def setUpClass(cls) -> None:
        S.clock = S.time.monotonic
        S.load_bank()
        cls.srv = S.Server(("127.0.0.1", 0), S.Handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.shutdown()
        cls.srv.server_close()

    def post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 200)
            return json.loads(r.read())

    def get(self, path: str) -> dict:
        with urllib.request.urlopen(self.base + path, timeout=30) as r:
            self.assertEqual(r.status, 200)
            return json.loads(r.read())

    def test_errors_are_http_200(self) -> None:
        self.assertEqual(self.post("/api/join", {"code": "QQQQ", "name": "x"}), {"ok": False, "error": "no_game"})
        self.assertFalse(self.get("/api/nope")["ok"])

    def test_long_poll_wakes_on_join(self) -> None:
        h = self.post("/api/create", {"name": "Host", "mode": 25})
        first = self.get(f"/api/state?g={h['code']}&p={h['token']}&v=0")
        result = {}

        def wait() -> None:
            result["snap"] = self.get(f"/api/state?g={h['code']}&p={h['token']}&v={first['v']}")

        waiter = threading.Thread(target=wait)
        waiter.start()
        self.post("/api/join", {"code": h["code"], "name": "Guest"})
        waiter.join(timeout=5)
        self.assertFalse(waiter.is_alive(), "long-poll should return as soon as someone joins")
        self.assertEqual([p["name"] for p in result["snap"]["players"]], ["Host", "Guest"])


if __name__ == "__main__":
    unittest.main()
