#!/usr/bin/env python3
"""Slot-level watcher. Opens the ticket page in headless Chrome, calls events/available with the site's own
reCAPTCHA v3 for one day, and sends a Telegram message for any new slot with a place for everyone in book_config.json. The day is `date` in book_config.json. Runs from cron under flock"""
import asyncio, json, os, subprocess, sys, time, urllib.request
from datetime import datetime, timezone

import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "book_config.json")))
TICKETS = len(CFG["people"])     # one named ticket per person
STATE = os.path.join(HERE, "slots_state.json")
PROFILE = os.path.join(HERE, "chrome-profile")
PORT = 9341
# product -> (name, slug, venue the site asks the time for first)
PRODUCTS = {
    4375: ("Regular", "4375-sagrada-familia", 1),
    4374: ("Guided tour", "4374-sagrada-familia-with-guided-tour", 1640),
    4443: ("Towers", "4443-sagrada-familia-with-towers", 3),
    4779: ("Guide + towers", "4779-sagrada-familia-with-guide-and-visit-to-the-towers", 1783),
}
# product -> second venue (basilica entry or tower for guided/tower products); queried against the chosen first slot
SECOND = {4374: 1, 4443: 1, 4779: 1784}
BOOKABLE = {4375, 4374, 4443, 4779}   # book.py can cart all of them


def notify(msg):
    tg = CFG["telegram"]
    r = urllib.request.Request(f"https://api.telegram.org/bot{tg['token']}/sendMessage", method="POST",
                               headers={"Content-Type": "application/json"},
                               data=json.dumps({"chat_id": tg["chat_id"], "text": msg}).encode())
    urllib.request.urlopen(r, timeout=20).read()


PAGE_JS = """
(async (products, day, min, second) => {
  const API = "https://services.clorian.com";
  const tok = (await (await fetch(`${API}/user/api/oauth/token?secretKey=thesagradafamiliafrontendoftomorrow`,
    {method: "POST", headers: {"Content-Type": "application/json", Accept: "application/json"}})).json()).access_token;
  // one token covers the whole run (the server accepts the same token again; tested 2026-10-26)
  const cap = await new Promise((ok, fail) => grecaptcha.ready(() =>
    grecaptcha.execute(serviceUrls.reCaptchaKey, {action: "getAvailability"}).then(ok, fail)));
  const out = [];
  for (const [pid, venueId] of products) {
    const q = new URLSearchParams({productId: pid, salesGroupId: 1, startDateFrom: day, startDateTo: day,
      venueId, recaptcha: cap});
    const r = await fetch(`${API}/catalog/events/available?${q}`,
      {headers: {Accept: "application/json", Authorization: `Bearer ${tok}`, pos: "649"}});
    if (!r.ok) throw new Error(`${pid}/${day}: HTTP ${r.status} ${(await r.text()).slice(0, 150)}`);
    for (const e of await r.json()) {
      if (e.status !== "enabled" || e.totalAvailability < min) continue;
      const slot = {pid, day, id: e.eventId, start: e.startDatetime.slice(11, 16), n: e.totalAvailability};
      if (second[pid]) {
        // the guide/tower slot is not enough: the linked second venue (15–45 min earlier) needs places too
        const q2 = new URLSearchParams({productId: pid, salesGroupId: 1, startDateFrom: day, startDateTo: day,
          venueId: second[pid], selectedEvents: JSON.stringify({[venueId]: e.eventId}), recaptcha: cap});
        const r2 = await fetch(`${API}/catalog/events/available?${q2}`,
          {headers: {Accept: "application/json", Authorization: `Bearer ${tok}`, pos: "649"}});
        if (!r2.ok) throw new Error(`${pid}/${day} second venue: HTTP ${r2.status} ${(await r2.text()).slice(0, 150)}`);
        const ok2 = (await r2.json()).filter(x => x.status === "enabled" && x.totalAvailability >= min);
        if (!ok2.length) continue;
        slot.also = ok2.map(x => x.startDatetime.slice(11, 16)).join("/");
      }
      out.push(slot);
    }
  }
  return JSON.stringify(out);
})
"""


async def run_in_chrome(day):
    chrome = subprocess.Popen(
        ["google-chrome", "--headless=new", "--disable-gpu", f"--remote-debugging-port={PORT}",
         f"--user-data-dir={PROFILE}", "--window-size=1400,1000",
         "https://tickets.sagradafamilia.org/en/1-individual/4375-sagrada-familia"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(30):
            try:
                pages = [t for t in json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json")) if t["type"] == "page"]
                if pages:
                    break
            except OSError:
                pass
            time.sleep(0.5)
        async with websockets.connect(pages[0]["webSocketDebuggerUrl"], max_size=None) as ws:
            n = 0

            async def ev(expr):
                nonlocal n
                n += 1
                await ws.send(json.dumps({"id": n, "method": "Runtime.evaluate",
                                          "params": {"expression": expr, "awaitPromise": True, "returnByValue": True}}))
                while True:
                    m = json.loads(await ws.recv())
                    if m.get("id") == n:
                        if "exceptionDetails" in m.get("result", {}):
                            raise RuntimeError(m["result"]["exceptionDetails"].get("exception", {}).get("description", m))
                        return m["result"]["result"].get("value")

            for _ in range(40):
                if await ev("!!(window.grecaptcha && window.grecaptcha.execute && window.serviceUrls && serviceUrls.reCaptchaKey)"):
                    break
                await asyncio.sleep(0.5)
            else:
                raise RuntimeError("page / reCAPTCHA did not load")
            prods = json.dumps([[pid, v] for pid, (_, _, v) in PRODUCTS.items()])
            return json.loads(await ev(f"{PAGE_JS}({prods}, {json.dumps(day)}, {TICKETS}, {json.dumps(SECOND)})"))
    finally:
        chrome.terminate()
        try:
            chrome.wait(10)
        except subprocess.TimeoutExpired:
            chrome.kill()


def main():
    day = CFG["date"]                # YYYY-MM-DD
    if day < datetime.now(timezone.utc).date().isoformat():
        return
    try:
        state = json.load(open(STATE))
    except Exception:
        state = {"open": [], "fails": 0}
    stamp = datetime.now().isoformat(timespec="seconds")
    try:
        free = asyncio.run(asyncio.wait_for(run_in_chrome(day), 120))
    except Exception as e:
        state["fails"] = state.get("fails", 0) + 1
        print(stamp, "ERROR", str(e)[:300], file=sys.stderr)
        if state["fails"] == 10:
            notify(f"Sagrada slot watcher failed 10 times in a row ({str(e)[:150]}). Check the site by hand.")
        json.dump(state, open(STATE, "w"))
        return

    if state.get("fails", 0) >= 10:
        notify("Sagrada slot watcher is working again.")
    now_open = [f"{s['pid']}|{s['id']}" for s in free]
    new = [s for s in free if f"{s['pid']}|{s['id']}" not in state.get("open", [])]
    by_prod = {}
    for s in new:
        by_prod.setdefault(s["pid"], []).append(s)
    for pid, slots in by_prod.items():
        name, slug, _ = PRODUCTS[pid]
        lines = "\n".join(f"• {day} {s['start']} ({s['n']} places)" + (f", basilica/tower {s['also']}" if s.get('also') else "") for s in slots)
        notify(f"🎟️ SAGRADA {name} — free slot:\n{lines}\n"
               f"https://tickets.sagradafamilia.org/en/1-individual/{slug}")
    # auto-cart the earliest new slot (one cart at a time; the site holds it 15 min)
    if new and time.time() > state.get("booked_until", 0):
        for s in sorted(new, key=lambda s: s["start"]):
            if s["pid"] in BOOKABLE:
                args = [sys.executable, os.path.join(HERE, "book.py"), str(s["pid"]), s["day"], s["start"]]
                if s.get("also"):
                    args.append(s["also"].split("/")[-1])
                r = subprocess.run(args, capture_output=True, text=True, timeout=200)
                print(stamp, "cart:", s["pid"], s["day"], s["start"], "→", (r.stdout + r.stderr).strip()[-200:])
                if r.returncode == 0:
                    state["booked_until"] = time.time() + 16 * 60
                break
    print(stamp, "free:", [f"{s['pid']} {s['day']} {s['start']}({s['n']})" for s in free] or "none",
          "new:", len(new))
    json.dump({"open": now_open, "fails": 0, "booked_until": state.get("booked_until", 0)}, open(STATE, "w"))


if __name__ == "__main__":
    main()
