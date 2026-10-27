#!/usr/bin/env python3
"""Adds a free slot to the cart, fills everything up to the payment page, leaves Chrome open and sends a Telegram message.
Usage: book.py <productId> <YYYY-MM-DD> <HH:MM> [basilica HH:MM]   (4375 regular, 4374 guided, 4443 towers, 4779 guide+towers)
Personal data lives in book_config.json (mode 600). The user connects via ssh tunnel + chrome://inspect and pays."""
import asyncio, base64, getpass, json, os, socket, subprocess, sys, tempfile, time, urllib.request

import websockets

from slots import CFG, TICKETS, notify

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 9351
PROFILE = os.path.join(tempfile.gettempdir(), "sagrada-book")
HOLD_SECONDS = 16 * 60          # the site holds the cart for 15 min
SLUGS = {4375: "4375-sagrada-familia", 4374: "4374-sagrada-familia-with-guided-tour",
         4443: "4443-sagrada-familia-with-towers", 4779: "4779-sagrada-familia-with-guide-and-visit-to-the-towers"}
TWO_STEP = {4374, 4443, 4779}   # guide/tower time first, then the linked second time


class Page:
    def __init__(self, ws):
        self.ws, self.n = ws, 0

    async def cmd(self, method, **params):
        self.n += 1
        i = self.n
        await self.ws.send(json.dumps({"id": i, "method": method, "params": params}))
        while True:
            m = json.loads(await self.ws.recv())
            if m.get("id") == i:
                if "error" in m:
                    raise RuntimeError(m["error"])
                return m.get("result", {})

    async def ev(self, expr):
        r = await self.cmd("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description", r))
        return r.get("result", {}).get("value")

    async def wait(self, expr, timeout=20, what=""):
        end = time.time() + timeout
        while time.time() < end:
            try:
                v = await self.ev(expr)
                if v:
                    return v
            except RuntimeError:
                pass        # the context drops while the page reloads
            await asyncio.sleep(0.3)
        raise RuntimeError(f"wait timed out: {what or expr[:80]}")

    async def key(self, k, code, vk, mod=0):
        for t in ("keyDown", "keyUp"):
            await self.cmd("Input.dispatchKeyEvent", type=t, key=k, code=code, windowsVirtualKeyCode=vk, modifiers=mod)

    async def type(self, selector, text):
        """Type with real keystrokes: the site does not register directly assigned values, and some fields reject paste."""
        await self.ev(f"(()=>{{const e=document.querySelector({json.dumps(selector)}); e.scrollIntoView({{block:'center'}}); e.focus();}})()")
        get = f"document.querySelector({json.dumps(selector)}).value"
        for delay in (0, 0.05, 0.15):       # repeatEmail drops characters when typed fast; verify, retry slower
            await self.key("a", "KeyA", 65, 2)
            await self.key("Backspace", "Backspace", 8)
            for ch in text:
                await self.cmd("Input.dispatchKeyEvent", type="keyDown", text=ch, key=ch, unmodifiedText=ch)
                await self.cmd("Input.dispatchKeyEvent", type="keyUp", key=ch)
                if delay:
                    await asyncio.sleep(delay)
            await asyncio.sleep(0.3)
            if (await self.ev(get) or "").replace(" ", "").endswith(text.replace(" ", "")[-6:]) and \
                    len((await self.ev(get) or "").replace(" ", "")) >= len(text.replace(" ", "")):
                break
        await self.ev(f"document.querySelector({json.dumps(selector)}).blur()")

    async def select(self, selector, value):
        await self.ev(f"""(()=>{{const e=document.querySelector({json.dumps(selector)});
            Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(e,{json.dumps(value)});
            e.dispatchEvent(new Event('change',{{bubbles:true}}));}})()""")

    async def click_text(self, text, sel="button"):
        return await self.ev(f"""(()=>{{const e=[...document.querySelectorAll({json.dumps(sel)})]
            .find(b=>b.offsetParent&&b.innerText&&b.innerText.trim()===({json.dumps(text)})); if(e){{e.click(); return true}} return false}})()""")


# slot times on the page (excluding language tabs)
TIMES = ("[...document.querySelectorAll('*')].filter(e=>e.children.length===0&&e.offsetParent"
         "&&/^\\d\\d:\\d\\d$/.test(e.innerText?.trim())&&!e.closest('.venuesCapacity-tabs')).map(e=>e.innerText.trim())")


async def click_time(page, hhmm):
    js = f"""(()=>{{const s=[...document.querySelectorAll('*')].find(e=>e.children.length===0&&e.offsetParent
        &&e.innerText?.trim()==={json.dumps(hhmm)}&&!e.closest('.venuesCapacity-tabs')); if(!s) return false; (s.closest('button')||s).click(); return true}})()"""
    for _ in range(10):         # can vanish while the page re-renders
        if await page.ev(js):
            await asyncio.sleep(1.5)
            return
        await asyncio.sleep(0.5)
    raise RuntimeError(f"slot {hhmm} not on the page")


TABS = "document.querySelectorAll('.venuesCapacity-tabs [role=button]')"


async def pick(page, want, exclude=(), strict=True):
    """In a tabbed step (guide language / tower) find and pick the wanted time; unless strict, fall back to the first available."""
    def opts(ts):
        return [t for t in (ts or []) if t not in exclude]
    n = await page.ev(f"{TABS}.length") or 0
    fallback = None
    for i in range(max(n, 1)):
        if n:
            await page.ev(f"{TABS}[{i}].click()")
            await asyncio.sleep(1)
        ts = opts(await page.ev(TIMES))
        if want in ts:
            return await click_time(page, want)
        if ts and fallback is None:
            fallback = (i, ts[0])
    if strict or fallback is None:
        raise RuntimeError(f"slot {want} not in any tab")
    if n:
        await page.ev(f"{TABS}[{fallback[0]}].click()")
        await asyncio.sleep(1)
    await click_time(page, fallback[1])


def field(fid, i):
    return f'[id$=".field-{fid}-bt-1-{i}[0].value"]'


async def book(pid, day, hhmm, second, page):
    y, m, d = day.split("-")
    # cookie banner: "Reject optional" may reload the page
    await page.wait("document.readyState==='complete' && !!document.querySelector('.CalendarDay')", 60, "calendar")
    if await page.click_text("Reject optional"):
        await asyncio.sleep(3)
        await page.wait("!!document.querySelector('.CalendarDay')", 30, "calendar (after cookies)")

    # go to the day in the calendar
    cell = f'[data-date-id="{d}/{m}/{y}"]'
    for _ in range(4):
        if await page.ev(f"document.querySelector('{cell}')?.closest('td')?.getAttribute('aria-disabled')==='false'"):
            break
        await page.ev("document.querySelector('[aria-label^=\"Move forward\"]')?.click()")
        await asyncio.sleep(2)
    await page.ev(f"document.querySelector('{cell}').closest('td').click()")

    await page.wait(TIMES + ".length>0", 20, "time list")
    if pid in TWO_STEP:
        # step 1: guide-language or tower tabs; step 2: linked basilica / tower time (may be tabbed too)
        await pick(page, hhmm)
        await page.wait(f"{TIMES}.filter(t=>t!=={json.dumps(hhmm)}).length>0", 20, "second time list")
        await asyncio.sleep(1)
        await pick(page, second, exclude=(hhmm,), strict=False)
    else:
        # first the range tab containing the time, then the time
        await page.ev(f"""(()=>{{const t={json.dumps(hhmm)};
            for(const b of document.querySelectorAll('button')){{const m=b.innerText.trim().match(/^(\\d\\d:\\d\\d) - (\\d\\d:\\d\\d)$/);
              if(m&&m[1]<=t&&t<=m[2]){{b.click();return}}}}}})()""")
        await asyncio.sleep(1)
        await click_time(page, hhmm)

    # N x General
    await page.wait("!!document.getElementById('quantity_1')", 20, "ticket quantity")
    for _ in range(6):
        v = int(await page.ev("document.getElementById('quantity_1').value"))
        if v == TICKETS:
            break
        await page.ev(f"""(()=>{{let r=document.getElementById('quantity_1'); while(r&&r.querySelectorAll('button').length<2) r=r.parentElement;
            const b=[...r.querySelectorAll('button')]; ({v}>{TICKETS}?b[0]:b[b.length-1]).click()}})()""")
        await asyncio.sleep(0.7)
    got = int(await page.ev("document.getElementById('quantity_1').value"))
    if got != TICKETS:
        raise RuntimeError(f"this slot only has places for {got}")
    await page.click_text("CONTINUE")

    # ticket holders (named, tickets are non-transferable)
    await page.wait(f"!!document.querySelector('{field(3711, 0)}')", 20, "name fields")
    for i, p in enumerate(CFG["people"]):
        await page.type(field(3711, i), p["name"])
        await page.type(field(3712, i), p["surname"])
        doc = p.get("doc_type", "PSP")
        await page.select(field(3876, i), doc)
        if doc == "PSP":                  # only passports ask for the issuing country
            await page.wait(f"!!document.querySelector('{field(3879, i)}')", 10, "issued by")
            await page.select(field(3879, i), p.get("issued_by", "OT"))
        # each document type / country shows its own number field; it is the person's only other text input
        number = f"""[...document.querySelectorAll('input[id*=".field-"][id$="-bt-1-{i}[0].value"]')]
            .find(e=>!/field-(3711|3712)-/.test(e.id))"""
        await page.wait(f"!!({number})", 10, "document number")
        await page.type(f"#{await page.ev(f'CSS.escape(({number}).id)')}", p["passport"])

    # add to cart (+ "No" to the upsell if shown)
    press = "(()=>{const c=[...document.querySelectorAll('button')].filter(b=>/CONTINUE/i.test(b.innerText)&&!b.disabled&&b.offsetParent); if(c.length) c[c.length-1].click()})()"
    await page.ev(press)
    end, last = time.time() + 45, time.time()
    while time.time() < end:
        if "summaryPurchase" in (await page.ev("location.href") or ""):
            break
        if await page.click_text("No"):
            last = time.time()
        elif time.time() - last > 8:      # the first click is sometimes lost (re-render after field blur); press again
            await page.ev(press)
            last = time.time()
        await asyncio.sleep(0.5)
    else:
        raise RuntimeError("could not add to cart (did not reach summaryPurchase)")

    # contact details + consent checkboxes
    c = CFG["contact"]
    await page.wait("!!document.getElementById('contact-firstName')", 20, "contact form")
    await page.type("#contact-firstName", c["name"])
    await page.type("#contact-lastName", c["surname"])
    await page.type("input[type=tel]", c["phone"])
    await page.type("#contact-email", c["email"])
    await page.type("#contact-repeatEmail", c["email"])
    await page.select("#contact-country", c["country"])
    await page.ev("['termsInput3','termsInput47'].forEach(id=>{const e=document.getElementById(id); if(e&&!e.checked) e.click()})")
    return await page.ev("document.body.innerText.match(/You have \\d+ minutes[^\\n]*/)?.[0] || ''")


async def main(pid, day, hhmm, second=None):
    subprocess.run(["pkill", "-f", f"[r]emote-debugging-port={PORT}"])
    time.sleep(1)
    subprocess.run(["rm", "-rf", PROFILE])
    chrome = subprocess.Popen(["google-chrome", "--headless=new", "--disable-gpu", f"--remote-debugging-port={PORT}",
                      f"--user-data-dir={PROFILE}", "--window-size=1300,1800",
                      f"https://tickets.sagradafamilia.org/en/1-individual/{SLUGS[pid]}"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(40):
        try:
            pages = [t for t in json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json")) if t["type"] == "page"]
            if pages:
                break
        except OSError:
            pass
        time.sleep(0.5)
    async with websockets.connect(pages[0]["webSocketDebuggerUrl"], max_size=None) as ws:
        page = Page(ws)
        try:
            left = await book(pid, day, hhmm, second, page)
        finally:
            shot = await page.cmd("Page.captureScreenshot", format="png")
            open(os.path.join(HERE, "book_last.png"), "wb").write(base64.b64decode(shot["data"]))
    # keep Chrome open; close it when the hold expires
    # kill only this run's Chrome (process group) so a stale timer cannot kill a newer cart
    subprocess.Popen(["bash", "-c", f"sleep {HOLD_SECONDS}; kill -- -{chrome.pid}"],
                     start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return left


if __name__ == "__main__":
    pid, day, hhmm = int(sys.argv[1]), sys.argv[2], sys.argv[3]
    second = sys.argv[4] if len(sys.argv) > 4 else None
    try:
        left = asyncio.run(asyncio.wait_for(main(pid, day, hhmm, second), 150))
    except Exception as e:
        notify(f"⚠️ SAGRADA: {day} {hhmm} could not add to cart: {str(e)[:200]}\nTry by hand now: "
               f"https://tickets.sagradafamilia.org/en/1-individual/{SLUGS.get(pid, pid)}")
        raise
    notify(f"🛒 SAGRADA IN CART: {day} {hhmm}, {TICKETS} people. {left}\n"
           f"Connect: ssh -N -L {PORT}:127.0.0.1:{PORT} {getpass.getuser()}@{socket.gethostname()} → chrome://inspect → inspect → PAY")
    print("ok", left)
