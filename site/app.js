/* Rewind quiz client. Talks to /api/* on the same host; state arrives by long-poll. */
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const DECADES = [1950, 1960, 1970, 1980, 1990, 2000];
  const LETTERS = ["A", "B", "C", "D"];
  const MAX_NAME = 16;

  const ERRORS = {
    name_required: "Type your name first.",
    bad_code: "The game code has four letters.",
    no_game: "No game with that code. Check the letters with the host.",
    game_full: "That game already has 20 players.",
    game_over: "That game has finished. Ask the host for the new code.",
    host_only: "Only the host can do that.",
    slow_down: "Too many new games from this network. Try again later.",
    server_busy: "The quiz server is full right now. Try again in a few minutes.",
    too_late: "Too late. Time ran out on that question.",
    already_started: "The game has already started.",
    not_in_game: "You are no longer in this game.",
    network: "Can't reach the quiz server. Check your connection and try again.",
  };
  const errorText = (code) => ERRORS[code] || "Something went wrong. Try again.";

  // ---------------------------------------------------------------- storage
  const store = {
    get(key) {
      try { return JSON.parse(localStorage.getItem("rewind." + key)); } catch { return null; }
    },
    set(key, value) {
      try { localStorage.setItem("rewind." + key, JSON.stringify(value)); } catch { /* private mode */ }
    },
    del(key) {
      try { localStorage.removeItem("rewind." + key); } catch { /* private mode */ }
    },
  };

  // ---------------------------------------------------------------- state
  let session = null; // { code, token, id }
  let snap = null; // last server snapshot
  let pollGen = 0;
  let pollCtrl = null;
  let mode = 25;
  let hofBoard = "25";
  let hofFrom = "home";
  let current = "home";
  let qrFor = "";
  let renderedQ = null; // "game:index" of the question on screen
  let revealedQ = null;
  let deadline = 0;
  let duration = 1;
  let nextAt = 0;
  let raf = 0;
  let nextTimer = 0;
  let wake = null;
  let chipIds = new Set();

  // ---------------------------------------------------------------- helpers
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const cleanCode = (s) => String(s || "").toUpperCase().replace(/[^A-Z]/g, "").slice(0, 4);
  const cleanName = (s) => String(s || "").replace(/\s+/g, " ").trim().slice(0, MAX_NAME);
  const fmt = (n) => Number(n || 0).toLocaleString("en-US");
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const buzz = (pattern) => {
    try { if (navigator.vibrate) navigator.vibrate(pattern); } catch { /* unsupported */ }
  };

  let toastTimer = 0;
  function toast(msg) {
    const t = $("toast");
    t.textContent = msg;
    t.hidden = false;
    t.style.animation = "none";
    void t.offsetWidth;
    t.style.animation = "";
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 3800);
  }

  function shake(input) {
    input.classList.remove("shake");
    void input.offsetWidth;
    input.classList.add("shake");
    input.focus();
  }

  function busy(btn, on) {
    btn.classList.toggle("busy", on);
    btn.setAttribute("aria-busy", on ? "true" : "false");
  }

  function show(name) {
    if (current === name) return;
    current = name;
    for (const s of document.querySelectorAll(".screen")) s.hidden = s.id !== "screen-" + name;
    window.scrollTo(0, 0);
  }

  async function keepAwake(on) {
    try {
      if (on && !wake && "wakeLock" in navigator && document.visibilityState === "visible") {
        wake = await navigator.wakeLock.request("screen");
        wake.addEventListener("release", () => { wake = null; });
      } else if (!on && wake) {
        await wake.release();
        wake = null;
      }
    } catch {
      wake = null;
    }
  }

  // ---------------------------------------------------------------- API
  async function post(path, body) {
    try {
      const r = await fetch("/api/" + path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        cache: "no-store",
      });
      return await r.json();
    } catch {
      return { ok: false, error: "network" };
    }
  }

  function act(path, extra) {
    return post(path, { code: session.code, token: session.token, ...(extra || {}) });
  }

  function setOffline(on) {
    $("net").hidden = !on;
  }

  async function pollLoop(gen) {
    let failures = 0;
    while (gen === pollGen && session) {
      const since = snap && snap.code === session.code ? snap.v : 0;
      const ctrl = new AbortController();
      pollCtrl = ctrl;
      const guard = setTimeout(() => ctrl.abort(), 40000);
      try {
        const url = `/api/state?g=${encodeURIComponent(session.code)}&p=${encodeURIComponent(session.token)}&v=${since}`;
        const r = await fetch(url, { signal: ctrl.signal, cache: "no-store" });
        const data = await r.json();
        clearTimeout(guard);
        if (gen !== pollGen) return;
        if (!data.ok) {
          if (data.error === "no_game" || data.error === "not_in_game") {
            // A stale saved game from an earlier visit ends quietly.
            endSession(snap ? (data.error === "no_game" ? "That game has ended." : errorText(data.error)) : "");
            return;
          }
          throw new Error(data.error);
        }
        failures = 0;
        setOffline(false);
        apply(data);
      } catch {
        clearTimeout(guard);
        if (gen !== pollGen) return;
        failures += 1;
        if (failures >= 2) setOffline(true);
        await sleep(Math.min(800 * failures, 5000));
      }
    }
  }

  function restartPoll() {
    pollGen += 1;
    if (pollCtrl) pollCtrl.abort();
    if (session) pollLoop(pollGen);
  }

  function startSession(res) {
    session = { code: res.code, token: res.token, id: res.id };
    store.set("session", session);
    snap = null;
    renderedQ = revealedQ = null;
    history.replaceState(null, "", "/?g=" + res.code);
    restartPoll();
  }

  function endSession(message) {
    session = null;
    snap = null;
    store.del("session");
    pollGen += 1;
    if (pollCtrl) pollCtrl.abort();
    cancelAnimationFrame(raf);
    clearInterval(nextTimer);
    setOffline(false);
    keepAwake(false);
    history.replaceState(null, "", "/");
    $("code").value = "";
    setInvited("");
    show("home");
    if (message) toast(message);
  }

  // ---------------------------------------------------------------- render
  function apply(s) {
    if (snap && s.v < snap.v && s.code === snap.code) return;
    const prev = snap;
    snap = s;
    keepAwake(true);
    if (s.phase === "lobby") {
      renderLobby(s);
      if (current !== "hof") show("lobby");
    } else if (s.phase === "question" || s.phase === "reveal") {
      renderPlay(s, prev);
      show("play");
    } else if (s.phase === "finished") {
      renderFinal(s, prev);
      if (current !== "hof") show("final");
    }
  }

  function hostName(s) {
    const h = s.players.find((p) => p.host);
    return h ? h.name : "the host";
  }

  function inviteUrl() {
    return `${location.origin}/?g=${session.code}`;
  }

  function renderLobby(s) {
    const code = $("lobby-code");
    if (code.dataset.code !== s.code) {
      code.dataset.code = s.code;
      code.replaceChildren(...[...s.code].map((ch) => el("span", "", ch)));
      code.setAttribute("aria-label", "Game code " + [...s.code].join(" "));
    }
    $("lobby-mode").textContent = `${s.mode} questions from the 1950s to the 2000s`;
    $("invite-url").textContent = inviteUrl().replace(/^https?:\/\//, "");
    if (qrFor !== s.code && window.qrcode) {
      const qr = window.qrcode(0, "M");
      qr.addData(inviteUrl());
      qr.make();
      $("qr").innerHTML = qr.createSvgTag({ cellSize: 4, margin: 0, scalable: true });
      qrFor = s.code;
    }

    const list = $("lobby-players");
    const seen = chipIds;
    chipIds = new Set(s.players.map((p) => p.id));
    list.replaceChildren(
      ...s.players.map((p) => {
        const cls = (p.id === s.you.id ? "me " : "") + (p.online ? "" : "away ") + (seen.has(p.id) ? "" : "new");
        const li = el("li", cls.trim());
        li.append(el("span", "", p.name));
        if (p.host) li.append(el("span", "tag", "Host"));
        else if (p.id === s.you.id) li.append(el("span", "tag", "You"));
        return li;
      }),
    );
    $("lobby-count").textContent = `${s.players.length} / ${s.max_players}`;

    const start = $("start-btn");
    start.hidden = !s.you.host;
    start.textContent = `Start game · ${s.mode} questions`;
    $("lobby-note").textContent = s.you.host
      ? s.players.length === 1
        ? "Share the code. You can also start on your own."
        : `${s.players.length} players are in. Start when everyone has joined.`
      : `Waiting for ${hostName(s)} to start the game.`;
  }

  function buildTrack(q) {
    const track = $("track");
    const key = q.plan.join(",");
    if (track.dataset.key !== key) {
      track.dataset.key = key;
      track.replaceChildren(
        ...DECADES.map((d, i) => {
          const seg = el("div", "seg-d d" + d);
          seg.style.flexGrow = String(q.plan[i] || 1);
          seg.append(el("i"));
          return seg;
        }),
      );
    }
    let start = 0;
    [...track.children].forEach((seg, i) => {
      const n = q.plan[i];
      const done = Math.min(n, Math.max(0, q.index + 1 - start));
      seg.firstChild.style.transform = `scaleX(${n ? done / n : 0})`;
      start += n;
    });
  }

  function renderPlay(s, prev) {
    const q = s.q;
    const key = `${s.game}:${q.index}`;
    const screen = $("screen-play");
    screen.dataset.decade = String(q.decade);
    $("qnum").textContent = `Question ${q.index + 1} of ${q.total}`;
    $("myscore").textContent = fmt(s.you.score);
    buildTrack(q);

    if (renderedQ !== key) {
      renderedQ = key;
      revealedQ = null;
      $("decade-tag").textContent = q.decade + "s";
      $("cat").textContent = q.cat;
      $("qtext").textContent = q.text;
      const box = $("answers");
      box.replaceChildren(
        ...q.options.map((text, i) => {
          const b = el("button", "answer");
          b.type = "button";
          b.dataset.i = String(i);
          b.append(el("span", "letter", LETTERS[i]), el("span", "txt", text));
          b.addEventListener("click", () => choose(i));
          return b;
        }),
      );
      const card = $("qcard");
      card.classList.remove("enter");
      void card.offsetWidth;
      card.classList.add("enter");
      if (s.phase === "question") buzz(12);
    }

    const buttons = [...$("answers").children];
    const mine = q.your_choice;

    screen.classList.toggle("revealing", s.phase === "reveal");
    $("play-note").hidden = s.phase === "reveal";
    if (s.phase === "question") {
      $("timer").hidden = false;
      $("result").hidden = true;
      $("reveal").hidden = true;
      clearInterval(nextTimer);
      buttons.forEach((b, i) => {
        b.disabled = mine !== null;
        b.classList.toggle("picked", mine === i);
        b.classList.remove("correct", "wrong", "dim");
        const v = b.querySelector(".votes");
        if (v) v.remove();
      });
      const waiting = Math.max(q.online, q.answered);
      $("play-note").textContent =
        mine === null ? "Tap your answer. Faster answers score more." : `Locked in. ${q.answered} of ${waiting} have answered.`;
      syncTimer(q.remaining_ms, q.duration_ms);
      return;
    }

    // reveal
    cancelAnimationFrame(raf);
    $("timer").hidden = true;
    const r = s.reveal;
    buttons.forEach((b, i) => {
      b.disabled = true;
      b.classList.toggle("picked", false);
      b.classList.toggle("correct", i === r.correct);
      b.classList.toggle("wrong", mine === i && i !== r.correct);
      b.classList.toggle("dim", i !== r.correct && mine !== i);
      let v = b.querySelector(".votes");
      if (!v) {
        v = el("span", "votes");
        b.append(v);
      }
      v.textContent = r.counts[i] ? `×${r.counts[i]}` : "";
    });

    const res = $("result");
    res.hidden = false;
    res.replaceChildren();
    if (mine === null) {
      res.className = "result none";
      res.append(el("span", "", "No answer this time"));
    } else if (mine === r.correct) {
      res.className = "result good";
      const streak = s.you.streak >= 3 ? ` · ${s.you.streak} in a row` : "";
      res.append(el("span", "", "Correct" + streak), el("b", "", "+" + fmt(r.your_points)));
    } else {
      res.className = "result bad";
      res.append(el("span", "", "Wrong answer"), el("b", "", "+0"));
    }
    if (revealedQ !== key) {
      revealedQ = key;
      buzz(mine === r.correct ? 30 : [50, 40, 50]);
    }

    $("play-note").textContent = "";
    $("fact").textContent = r.fact;
    $("reveal").hidden = false;
    renderBoard($("mini-board"), s, 5, true);
    $("board-count").textContent = `You are ${ordinal(s.you.rank)} of ${s.players.length}`;

    const next = $("next-btn");
    next.hidden = !s.you.host;
    next.textContent = r.last ? "Show final scores" : "Next question";
    nextAt = performance.now() + r.next_ms;
    const tickNext = () => {
      const left = Math.max(0, Math.ceil((nextAt - performance.now()) / 1000));
      $("next-note").textContent = r.last ? `Final scores in ${left} s` : `Next question in ${left} s`;
    };
    tickNext();
    clearInterval(nextTimer);
    nextTimer = setInterval(tickNext, 250);
  }

  function syncTimer(remaining, total) {
    const target = performance.now() + remaining;
    duration = total;
    if (Math.abs(target - deadline) > 400) deadline = target;
    cancelAnimationFrame(raf);
    const fill = $("timer-fill");
    const num = $("timer-num");
    const timer = $("timer");
    const frame = () => {
      const left = Math.max(0, deadline - performance.now());
      fill.style.transform = `scaleX(${left / duration})`;
      num.textContent = String(Math.ceil(left / 1000));
      timer.classList.toggle("hurry", left <= 5000);
      if (left > 0 && snap && snap.phase === "question") raf = requestAnimationFrame(frame);
    };
    frame();
  }

  function ordinal(n) {
    const s = ["th", "st", "nd", "rd"];
    const v = n % 100;
    return n + (s[(v - 20) % 10] || s[v] || s[0]);
  }

  function boardRow(p, s, withGain) {
    const li = el("li", (p.id === s.you.id ? "me " : "") + (p.left || !p.online ? "away" : ""));
    li.append(el("span", "rank", String(p.rank)));
    const who = el("span", "who");
    who.append(el("span", "name", p.name + (p.id === s.you.id ? " (you)" : "")));
    const meta = el("span", "meta");
    const moved = p.prev_rank - p.rank;
    if (withGain && moved > 0) meta.append(el("span", "up", `▲ ${moved} `));
    if (withGain && moved < 0) meta.append(el("span", "down", `▼ ${-moved} `));
    meta.append(document.createTextNode(`${p.correct} right` + (p.left ? " · left" : "")));
    who.append(meta);
    li.append(who);
    const pts = el("span", "pts", fmt(p.score));
    if (withGain) pts.append(el("span", "gain" + (p.gained ? "" : " zero"), "+" + fmt(p.gained)));
    li.append(pts);
    return li;
  }

  function renderBoard(list, s, limit, withGain) {
    const rows = (limit ? s.players.slice(0, limit) : s.players).map((p) => boardRow(p, s, withGain));
    const meIndex = s.players.findIndex((p) => p.id === s.you.id);
    if (limit && meIndex >= limit) {
      if (meIndex > limit) rows.push(el("li", "gap", "···"));
      rows.push(boardRow(s.players[meIndex], s, withGain));
    }
    list.replaceChildren(...rows);
  }

  function renderFinal(s, prev) {
    cancelAnimationFrame(raf);
    clearInterval(nextTimer);
    const f = s.final;
    const top = s.players.filter((p) => p.rank === 1);
    const title = $("final-title");
    if (top.length > 1) title.textContent = "It's a tie!";
    else if (top[0] && top[0].id === s.you.id) title.textContent = "You win!";
    else title.textContent = top[0] ? `${top[0].name} wins!` : "Game over";
    $("final-sub").textContent =
      `You finished ${ordinal(s.you.rank)} of ${s.players.length} with ${fmt(s.you.score)} points and ${s.you.correct} of ${f.total} right.`;

    const firstRender = !prev || prev.phase !== "finished" || prev.game !== s.game;
    if (firstRender) {
      const podium = $("podium");
      const colours = { 1: "d1970", 2: "d2000", 3: "d1960" };
      podium.replaceChildren(
        ...[1, 0, 2].map((pos) => {
          const p = s.players[pos];
          const step = el("div", "step p" + (pos + 1) + (p ? "" : " empty"));
          step.append(el("span", "pname", p ? p.name : "-"));
          step.append(el("span", "ppts", p ? fmt(p.score) : "0"));
          step.append(el("div", "block " + colours[pos + 1], p ? String(p.rank) : ""));
          return step;
        }),
      );
      if (s.you.rank === 1) buzz([40, 60, 40, 60, 120]);
    }
    renderBoard($("final-board"), s, 0, false);

    $("decade-bars").replaceChildren(
      ...f.by_decade.map((d) => {
        const li = el("li", "d" + d.decade);
        li.append(el("span", "dl", d.decade + "s"));
        const bar = el("span", "bar");
        const fill = el("i");
        fill.style.width = d.asked ? `${(100 * d.right) / d.asked}%` : "0";
        bar.append(fill);
        li.append(bar, el("span", "dv", `${d.right}/${d.asked}`));
        return li;
      }),
    );
    const note = $("hof-note");
    note.hidden = !f.hof_rank;
    if (f.hof_rank) note.textContent = `Your score is number ${f.hof_rank} in the all-time Hall of Fame for ${s.mode}-question games.`;

    $("again-btn").hidden = !s.you.host;
    $("again-note").textContent = s.you.host
      ? "Everyone stays in the game. The rematch uses questions you have not had yet."
      : `Waiting for ${hostName(s)} to start a rematch.`;
  }

  // ---------------------------------------------------------------- actions
  async function choose(i) {
    if (!snap || snap.phase !== "question" || snap.q.your_choice !== null) return;
    const index = snap.q.index;
    snap.q.your_choice = i;
    renderPlay(snap, snap);
    buzz(15);
    const res = await act("answer", { index, choice: i });
    if (res.ok || res.error === "already_answered") return;
    if (snap && snap.phase === "question" && snap.q.index === index && res.error === "network") {
      snap.q.your_choice = null;
      renderPlay(snap, snap);
    }
    toast(errorText(res.error));
  }

  function readName() {
    const input = $("name");
    const name = cleanName(input.value);
    if (!name) {
      shake(input);
      toast(errorText("name_required"));
      return "";
    }
    store.set("name", name);
    return name;
  }

  async function hostGame(e) {
    e.preventDefault();
    const name = readName();
    if (!name) return;
    const btn = $("host-btn");
    busy(btn, true);
    const res = await post("create", { name, mode });
    busy(btn, false);
    if (!res.ok) return toast(errorText(res.error));
    startSession(res);
  }

  async function joinGame(e) {
    e.preventDefault();
    const codeInput = $("code");
    const code = cleanCode(codeInput.value);
    if (code.length !== 4) {
      shake(codeInput);
      return toast(errorText("bad_code"));
    }
    const name = readName();
    if (!name) return;
    const btn = $("join-btn");
    busy(btn, true);
    const saved = store.get("session");
    const token = saved && saved.code === code ? saved.token : undefined;
    const res = await post("join", { code, name, token });
    busy(btn, false);
    if (!res.ok) return toast(errorText(res.error));
    startSession(res);
  }

  async function simple(btn, path) {
    busy(btn, true);
    const res = await act(path);
    busy(btn, false);
    if (!res.ok) toast(errorText(res.error));
  }

  async function leave() {
    if (session) act("leave");
    endSession();
  }

  async function share() {
    const url = inviteUrl();
    if (navigator.share) {
      try {
        await navigator.share({ title: "Rewind quiz", text: `Join my Rewind quiz. The game code is ${session.code}.`, url });
        return;
      } catch (err) {
        if (err && err.name === "AbortError") return;
      }
    }
    copy();
  }

  async function copy() {
    const url = inviteUrl();
    try {
      await navigator.clipboard.writeText(url);
      toast("Invite link copied");
    } catch {
      const range = document.createRange();
      range.selectNodeContents($("invite-url"));
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      toast("Select and copy the link above");
    }
  }

  // ---------------------------------------------------------------- hall of fame
  async function openHof(from) {
    hofFrom = from;
    show("hof");
    await loadHof();
  }

  async function loadHof() {
    const list = $("hof-list");
    list.replaceChildren(el("li", "gap", "Loading…"));
    $("hof-empty").hidden = true;
    let data;
    try {
      data = await (await fetch("/api/leaderboard", { cache: "no-store" })).json();
    } catch {
      data = { ok: false };
    }
    if (!data.ok) {
      list.replaceChildren();
      return toast(errorText("network"));
    }
    const rows = data.boards[hofBoard] || [];
    list.replaceChildren(
      ...rows.map((e, i) => {
        const li = el("li");
        li.append(el("span", "rank", String(i + 1)));
        const who = el("span", "who");
        who.append(el("span", "name", e.name));
        who.append(el("span", "meta", `${e.correct}/${e.total} right · ${e.players} player${e.players === 1 ? "" : "s"} · ${e.date}`));
        li.append(who, el("span", "pts", fmt(e.score)));
        return li;
      }),
    );
    $("hof-empty").hidden = rows.length > 0;
  }

  function closeHof() {
    if (session && snap) {
      current = "hof";
      show(snap.phase === "lobby" ? "lobby" : snap.phase === "finished" ? "final" : "play");
    } else {
      show(hofFrom === "final" ? "home" : hofFrom);
    }
  }

  // ---------------------------------------------------------------- setup
  function setInvited(code) {
    $("join-title").textContent = code ? `You're invited to game ${code}` : "Join a game";
    $("join-form").classList.toggle("invited", Boolean(code));
    const home = $("screen-home");
    // Put the join form first when someone arrives from an invite link.
    if (code) home.insertBefore($("join-form"), $("host-form"));
  }

  function selectMode(btn) {
    mode = Number(btn.dataset.mode);
    for (const b of document.querySelectorAll("#host-form [data-mode]")) {
      b.setAttribute("aria-checked", b === btn ? "true" : "false");
    }
  }

  function init() {
    $("name").value = store.get("name") || "";
    $("code").addEventListener("input", (e) => {
      const v = cleanCode(e.target.value);
      if (v !== e.target.value) e.target.value = v;
    });
    $("join-form").addEventListener("submit", joinGame);
    $("host-form").addEventListener("submit", hostGame);
    for (const b of document.querySelectorAll("#host-form [data-mode]")) b.addEventListener("click", () => selectMode(b));
    $("start-btn").addEventListener("click", (e) => simple(e.currentTarget, "start"));
    $("next-btn").addEventListener("click", (e) => simple(e.currentTarget, "next"));
    $("again-btn").addEventListener("click", (e) => simple(e.currentTarget, "again"));
    $("leave-btn").addEventListener("click", leave);
    $("home-btn").addEventListener("click", leave);
    $("share-btn").addEventListener("click", share);
    $("copy-btn").addEventListener("click", copy);
    $("hof-btn").addEventListener("click", () => openHof("home"));
    $("final-hof-btn").addEventListener("click", () => openHof("final"));
    $("hof-back").addEventListener("click", closeHof);
    for (const b of document.querySelectorAll("#screen-hof [data-board]")) {
      b.addEventListener("click", () => {
        hofBoard = b.dataset.board;
        for (const o of document.querySelectorAll("#screen-hof [data-board]")) {
          o.setAttribute("aria-selected", o === b ? "true" : "false");
        }
        loadHof();
      });
    }
    if (!navigator.share) {
      $("share-btn").hidden = true;
      $("copy-btn").classList.remove("ghost");
    }

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && session) {
        keepAwake(true);
        restartPoll();
      }
    });
    window.addEventListener("pageshow", (e) => {
      if (e.persisted && session) restartPoll();
    });
    window.addEventListener("online", () => { if (session) restartPoll(); });

    const invited = cleanCode(new URLSearchParams(location.search).get("g"));
    const saved = store.get("session");
    if (saved && saved.code && saved.token && (!invited || invited === saved.code)) {
      session = saved;
      restartPoll();
    } else if (invited.length === 4) {
      $("code").value = invited;
      setInvited(invited);
      if (!$("name").value) $("name").focus();
    }
  }

  init();
})();
