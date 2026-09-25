// End-to-end run: one host (iPhone) and two players (Android) play a full game.
//
//   QUIZ_STATIC=site QUIZ_PORT=8799 python3 server/server.py &
//   NODE_PATH=$(npm root -g) node tests/e2e.js http://127.0.0.1:8799 /tmp/shots
//
// Uses Chromium with phone emulation (touch, viewport, user agent).
const { chromium, devices } = require("playwright");
const fs = require("fs");
const path = require("path");

const BASE = process.argv[2] || "http://127.0.0.1:8799";
const OUT = process.argv[3] || "shots";
fs.mkdirSync(OUT, { recursive: true });

const shot = (page, name) => page.screenshot({ path: path.join(OUT, name + ".png"), fullPage: true });
const expect = (cond, msg) => {
  if (!cond) throw new Error("FAILED: " + msg);
  console.log("ok -", msg);
};

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM || undefined });
  const phone = async (device) => {
    const ctx = await browser.newContext({ ...devices[device], deviceScaleFactor: 2 });
    const page = await ctx.newPage();
    page.on("pageerror", (e) => { throw new Error(`${device} page error: ${e.message}`); });
    return page;
  };
  const host = await phone("iPhone 13");
  const anna = await phone("Pixel 7");
  const ola = await phone("Pixel 7");

  // Host creates a 25-question game.
  await host.goto(BASE + "/");
  await host.evaluate(() => document.fonts.ready);
  await shot(host, "01-home");
  await host.tap("#host-btn");
  expect(await host.isVisible("#toast"), "creating without a name shows a message");
  await host.fill("#name", "Mats");
  await host.tap("#host-btn");
  await host.waitForSelector("#screen-lobby:not([hidden])");
  const code = await host.getAttribute("#lobby-code", "data-code");
  expect(/^[A-Z]{4}$/.test(code), `game created with code ${code}`);
  expect(await host.isVisible("#qr svg"), "lobby shows a QR code");
  expect(new URL(host.url()).search === "?g=" + code, "host URL carries the game code");

  // Anna follows the invite link; Ola types the code.
  await anna.goto(`${BASE}/?g=${code}`);
  expect((await anna.textContent("#join-title")).includes(code), "invite link pre-fills the join form");
  await anna.fill("#name", "Anna");
  await shot(anna, "02-invited");
  await anna.tap("#join-btn");
  await anna.waitForSelector("#screen-lobby:not([hidden])");

  await ola.goto(BASE + "/");
  await ola.fill("#name", "Ola");
  await ola.fill("#code", "zzzz");
  await ola.tap("#join-btn");
  await ola.waitForSelector("#toast:not([hidden])");
  expect((await ola.textContent("#toast")).includes("No game"), "wrong code gives a clear message");
  await ola.fill("#code", code.toLowerCase());
  await ola.tap("#join-btn");
  await ola.waitForSelector("#screen-lobby:not([hidden])");

  await host.waitForFunction(() => document.querySelectorAll("#lobby-players li").length === 3);
  expect(true, "host sees all three players in the lobby");
  expect(!(await anna.isVisible("#start-btn")), "only the host has a start button");
  await shot(host, "03-lobby-host");
  await shot(anna, "04-lobby-player");

  // Start.
  await host.tap("#start-btn");
  for (const p of [host, anna, ola]) await p.waitForSelector("#screen-play:not([hidden]) #timer:not([hidden])");
  expect((await anna.textContent("#qnum")) === "Question 1 of 25", "everyone gets question 1 of 25");
  await anna.waitForTimeout(600);
  await shot(anna, "05-question");

  // Question 1: Anna answers first and waits; then the others answer and it reveals early.
  await anna.tap("#answers .answer >> nth=0");
  await anna.waitForFunction(() => document.querySelector("#play-note").textContent.startsWith("Locked in"));
  await shot(anna, "06-locked-in");
  await host.tap("#answers .answer >> nth=1");
  await ola.tap("#answers .answer >> nth=2");
  for (const p of [host, anna, ola]) await p.waitForSelector("#reveal:not([hidden])");
  expect(true, "question closes as soon as everyone has answered");
  expect(await host.isVisible("#next-btn"), "host can skip to the next question");
  expect(!(await anna.isVisible("#next-btn")), "players wait for the countdown");
  expect((await host.locator("#answers .answer.correct").count()) === 1, "one answer is marked correct");
  await shot(anna, "07-reveal");
  await shot(host, "08-reveal-host");

  // Reload Anna mid-game: she should drop straight back in.
  await host.tap("#next-btn");
  await anna.reload();
  await anna.waitForSelector("#screen-play:not([hidden])");
  expect((await anna.textContent("#qnum")) === "Question 2 of 25", "reload rejoins the running game");

  // Play the rest quickly; each player taps a different option.
  for (let i = 1; i < 25; i++) {
    const n = `Question ${i + 1} of 25`;
    for (const p of [host, anna, ola]) {
      await p.waitForFunction((t) => document.querySelector("#qnum").textContent === t && !document.querySelector("#timer").hidden, n);
    }
    if (i === 12) {
      await host.waitForTimeout(400);
      await shot(host, "09-question-80s");
    }
    await host.tap("#answers .answer >> nth=" + (i % 4));
    await anna.tap("#answers .answer >> nth=" + ((i + 1) % 4));
    await ola.tap("#answers .answer >> nth=" + ((i + 2) % 4));
    await host.waitForSelector("#reveal:not([hidden])");
    await host.tap("#next-btn");
  }

  for (const p of [host, anna, ola]) await p.waitForSelector("#screen-final:not([hidden])");
  expect(true, "all three reach the final scores");
  const rows = await host.locator("#final-board li").count();
  expect(rows === 3, "final board lists every player");
  expect(await host.isVisible("#again-btn"), "host can start a rematch");
  await anna.waitForTimeout(700);
  await shot(host, "10-final-host");
  await shot(anna, "11-final-player");

  await anna.tap("#final-hof-btn");
  await anna.waitForSelector("#hof-list li .pts");
  expect((await anna.locator("#hof-list li").count()) >= 3, "hall of fame lists the finished game");
  await shot(anna, "12-hall-of-fame");
  await anna.tap("#hof-back");

  // Rematch brings everyone back to the lobby.
  await host.tap("#again-btn");
  for (const p of [host, anna, ola]) await p.waitForSelector("#screen-lobby:not([hidden])");
  expect(true, "rematch returns everyone to the lobby");

  await ola.tap("#leave-btn");
  await ola.waitForSelector("#screen-home:not([hidden])");
  await host.waitForFunction(() => document.querySelectorAll("#lobby-players li").length === 2);
  expect(true, "a player can leave and the lobby updates");

  await browser.close();
  console.log("\nE2E PASSED");
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
