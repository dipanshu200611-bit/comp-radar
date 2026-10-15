#!/usr/bin/env python3
"""
comp_radar.py - one bot that finds quant/finance, consulting and product-management
competitions across the web, keeps a database, serves a live dashboard, and alerts you.

No site list anywhere: it generates search queries (topic x phrasing x region x current month),
searches the open web, reads every result page, follows listing pages one level deep, and
extracts mode (online/hybrid/in-person), eligibility, deadline and registration link.

  pip install -r requirements.txt
  python comp_radar.py                          scan once (all categories)
  python comp_radar.py --serve                  scan if empty + dashboard at http://127.0.0.1:8000
  python comp_radar.py --serve --daily 08:00    full always-on mode (dashboard + daily scan + alerts)
  python comp_radar.py --profile product --include-offline

Config (env vars or a .env file next to this script):
  ANTHROPIC_API_KEY   Claude reads each page (much more accurate than regex)   CLAUDE_MODEL (default claude-sonnet-5-5)
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID    DISCORD_WEBHOOK      push alerts
  EXTRA_QUERIES       comma-separated extra searches       MAX_PAGES (default 200 per category)
  WORKERS (default 6) SEARCH_DELAY (default 2 seconds)     REMIND_DAYS (default 3)
"""
import argparse, csv, functools, json, os, re, threading, time, datetime as dt
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_F, META_F, CSV_F, MD_F = (DATA_DIR / n for n in ("competitions.json", "meta.json", "competitions.csv", "competitions.md"))


def load_env():
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
UA = {"User-Agent": "Mozilla/5.0 (compatible; comp-radar/1.0; personal use)"}
MAX_PAGES = int(os.getenv("MAX_PAGES", 200))
WORKERS = int(os.getenv("WORKERS", 6))
SEARCH_DELAY = float(os.getenv("SEARCH_DELAY", 2))
REMIND_DAYS = int(os.getenv("REMIND_DAYS", 3))
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")

REGIONS = ["India", "USA", "UK", "Europe", "Singapore Asia", "Middle East", "Africa", "Australia", "Latin America", "global"]
PHRASES = ["registration open", "apply now deadline"]
PROFILES = {
    "finance": {
        "label": "quant / finance / trading competition",
        "online_default": True,
        "topics": ["quant trading competition", "algorithmic trading competition", "finance case competition", "stock market simulation competition",
                   "quantitative finance challenge", "investment banking case competition", "equity research competition", "financial modelling competition",
                   "portfolio management challenge", "fintech hackathon", "trading challenge university students", "crypto trading competition",
                   "hedge fund stock pitch competition", "risk management case competition", "market making competition"],
        "domain_re": r"quant|trading|finance|financial|investment|equity|portfolio|fintech|stock|banking|hedge|crypto|economics",
    },
    "consulting": {
        "label": "consulting / strategy case competition",
        "online_default": False,
        "topics": ["consulting case competition", "strategy case competition", "business case competition students",
                   "management consulting case challenge", "MBA case competition", "undergraduate business case competition"],
        "domain_re": r"consult|strategy|business case|case (?:competition|challenge|study)|client|recommendation|framework|management",
    },
    "product": {
        "label": "product management case competition",
        "online_default": False,
        "topics": ["product management case competition", "product case competition students", "product strategy challenge",
                   "product sense competition", "APM case competition", "product design case challenge"],
        "domain_re": r"product (?:management|manager|strategy|sense|design|case|roadmap)|\bapm\b|go-to-market|user research|roadmap|prd|\bpm\b",
    },
}

COMP_WORDS = r"competition|challenge|hackathon|case study|case comp|championship|contest|tournament|olympiad|pitch|simulation"
CLOSED = ["registration closed", "registrations closed", "registration is closed", "entries closed",
          "applications closed", "registration has closed", "submissions closed", "this competition has ended"]
MONTHS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
D1 = re.compile(rf"(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTHS})[a-z]*\.?,?\s+(20\d\d)", re.I)
D2 = re.compile(rf"({MONTHS})[a-z]*\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(20\d\d)", re.I)
CUE = re.compile(r"deadline|register by|registration (?:closes|ends|deadline)|last date|apply by|applications? close|closes? on|ends? on", re.I)


# ------------------------------------------------------------------ extraction
def build_queries(profile):
    p, month = PROFILES[profile], dt.date.today().strftime("%B %Y")
    qs = [f"{t} {ph} {month}" for t in p["topics"] for ph in PHRASES]
    qs += [f"{t} {r} {month}" for t in p["topics"][:4] for r in REGIONS]
    qs += [q.strip() for q in os.getenv("EXTRA_QUERIES", "").split(",") if q.strip()]
    return qs


def dates_in(text):
    pairs = list(D1.findall(text)) + [(d, m, y) for m, d, y in D2.findall(text)]
    out = []
    for d, m, y in pairs:
        try:
            out.append(dt.datetime.strptime(f"{d} {m[:3].title()} {y}", "%d %b %Y").date())
        except ValueError:
            pass
    return out


def find_deadline(text):
    found = []
    for m in CUE.finditer(text):
        found += dates_in(text[m.start(): m.end() + 90])
    return max(found) if found else None


def detect_mode(low):
    online = bool(re.search(r"\b(online|virtual|remote)\b", low))
    onsite = bool(re.search(r"in[- ]person|on[- ]campus|venue|offline|on-site", low))
    return "hybrid" if online and onsite else "online" if online else "in-person" if onsite else "unknown"


def heuristic_extract(url, soup, text, profile):
    low = text.lower()
    hits = len(re.findall(PROFILES[profile]["domain_re"], low))
    reg = url
    for a in soup.find_all("a", href=True):
        if re.search(r"register|apply|sign\s*up|enrol|join (?:now|the)", a.get_text(" "), re.I):
            reg = urljoin(url, a["href"])
            break
    return {"is_competition": bool(re.search(COMP_WORDS, low)) and hits >= 3, "mode": detect_mode(low),
            "eligibility": "students" if re.search(r"student|undergrad|mba|university|college", low) else "",
            "closed": any(c in low for c in CLOSED), "title": (soup.title.get_text(" ").strip() if soup.title else url)[:140],
            "organizer": "", "deadline": str(find_deadline(text) or "") or None, "registration_url": reg}


def claude_extract(url, text, profile):
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    prompt = (f"Today is {dt.date.today()}. Below is text from {url}.\nDecide if this page announces a specific "
              f"{PROFILES[profile]['label']} (a real, specific event, not a blog post or generic article). Reply with ONLY JSON: "
              '{"is_competition":bool,"mode":"online"|"hybrid"|"in-person"|"unknown","eligibility":str,"closed":bool,'
              '"title":str,"organizer":str,"deadline":"YYYY-MM-DD" or null,"registration_url":str or null}\n\n' + text[:8000])
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=60,
                          headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                          json={"model": MODEL, "max_tokens": 400, "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        return json.loads(re.sub(r"```json|```", "", r.json()["content"][0]["text"]).strip())
    except Exception as e:
        print(f"  [claude] {e}")
        return None


def norm(url):
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc.lower()}{p.path.rstrip('/')}"


def fetch(url):
    try:
        r = requests.get(url, headers=UA, timeout=20)
        if "html" not in r.headers.get("content-type", "") or r.status_code >= 400:
            return None, ""
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "noscript", "nav", "footer"]):
            t.decompose()
        return soup, " ".join(soup.get_text(" ").split())
    except Exception:
        return None, ""


def listing_links(soup, base, profile, cap=15):
    out = []
    for a in soup.find_all("a", href=True):
        t = a.get_text(" ")
        if re.search(COMP_WORDS, t, re.I) and re.search(PROFILES[profile]["domain_re"], t, re.I):
            out.append(urljoin(base, a["href"]))
    return list(dict.fromkeys(out))[:cap]


def analyze(url, profile):
    soup, text = fetch(url)
    if not soup or len(text) < 200:
        return None, []
    info = claude_extract(url, text, profile) or heuristic_extract(url, soup, text, profile)
    links = listing_links(soup, url, profile)
    if not info.get("is_competition") or info.get("closed"):
        return None, links
    dl = info.get("deadline")
    if dl and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(dl)):
        dl = None
    if dl and dl < str(dt.date.today()):
        return None, links
    reg = info.get("registration_url") or url
    if not str(reg).startswith("http"):
        reg = urljoin(url, str(reg))
    return {"title": (info.get("title") or url)[:140], "organizer": info.get("organizer") or "", "mode": info.get("mode") or "unknown",
            "eligibility": info.get("eligibility") or "", "deadline": dl, "registration_url": reg, "page": url}, links


def discover(profile, online_only):
    seen, seeds = set(), []
    with DDGS() as ddgs:
        for q in build_queries(profile):
            try:
                for hit in ddgs.text(q, max_results=12, timelimit="m"):
                    u = hit.get("href") or hit.get("url")
                    if u and norm(u) not in seen:
                        seen.add(norm(u))
                        seeds.append(u)
            except Exception as e:
                print(f"[search] {q[:50]}... {e}")
            time.sleep(SEARCH_DELAY)
    print(f"[{profile}] {len(seeds)} seed pages")

    results, budget, batch, depth = {}, MAX_PAGES, seeds, 0
    while batch and budget > 0 and depth < 2:
        batch = batch[:budget]
        budget -= len(batch)
        nxt = []
        with ThreadPoolExecutor(WORKERS) as ex:
            for info, links in ex.map(lambda u: analyze(u, profile), batch):
                if info and not (online_only and info["mode"] not in ("online", "hybrid")):
                    results[norm(info["registration_url"])] = info
                    print(f"  + {info['title'][:60]} | {info['mode']} | {info['deadline']}")
                if depth == 0:
                    for l in links:
                        if norm(l) not in seen:
                            seen.add(norm(l))
                            nxt.append(l)
        batch, depth = nxt, depth + 1
    return results


# ------------------------------------------------------------------ storage + alerts
def notify(text):
    tok, chat, hook = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID"), os.getenv("DISCORD_WEBHOOK")
    try:
        if tok and chat:
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          data={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True}, timeout=20)
        if hook:
            requests.post(hook, json={"content": text[:1900]}, timeout=20)
    except Exception as e:
        print(f"[notify] {e}")


def load_db():
    return json.loads(DB_F.read_text()) if DB_F.exists() else {}


def fmt(v):
    return f"{v['title'][:80]} [{v['category']}/{v['mode']}] ({v['deadline'] or 'no deadline'})\n{v['registration_url']}"


def store_results(found_by_profile):
    DATA_DIR.mkdir(exist_ok=True)
    today = str(dt.date.today())
    db, new = load_db(), []
    for prof, found in found_by_profile.items():
        for k, v in found.items():
            if k not in db:
                db[k] = {**v, "category": prof, "first_seen": today, "reminded": False}
                new.append(k)
            else:
                db[k].update(v, category=db[k]["category"])
            db[k]["last_seen"] = today
    stale = (dt.date.today() - dt.timedelta(days=45)).isoformat()
    db = {k: v for k, v in db.items() if (v.get("deadline") or "9999") >= today and (v.get("deadline") or v["last_seen"] >= stale)}

    soon = [k for k, v in db.items() if v.get("deadline") and not v["reminded"]
            and (dt.date.fromisoformat(v["deadline"]) - dt.date.today()).days <= REMIND_DAYS]
    if new:
        notify("New competitions:\n\n" + "\n\n".join(fmt(db[k]) for k in new[:12]))
    if soon:
        notify(f"Closing within {REMIND_DAYS} days:\n\n" + "\n\n".join(fmt(db[k]) for k in soon[:12]))
        for k in soon:
            db[k]["reminded"] = True

    DB_F.write_text(json.dumps(db, indent=1))
    rows = sorted(db.values(), key=lambda v: v.get("deadline") or "9999")
    MD_F.write_text(f"# Competitions (updated {today})\n\n" + "\n".join(
        f"- **{v['title']}** | {v['category']} | {v['mode']} | {v['eligibility'] or 'n/a'} | deadline: {v['deadline'] or 'n/a'} | {v['registration_url']}" for v in rows), encoding="utf-8")
    with CSV_F.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["category", "title", "organizer", "mode", "eligibility", "deadline", "registration_url", "found_on", "first_seen"])
        for v in rows:
            w.writerow([v["category"], v["title"], v["organizer"], v["mode"], v["eligibility"], v["deadline"] or "", v["registration_url"], v["page"], v["first_seen"]])
    return db, new


# ------------------------------------------------------------------ scan orchestration
STATUS = {"running": False, "last_run": None, "last_new": 0, "total": 0}
LOCK = threading.Lock()


def scan(profiles, online_flag):
    if not LOCK.acquire(blocking=False):
        return False
    STATUS["running"] = True
    try:
        found = {}
        for p in profiles:
            only = PROFILES[p]["online_default"] if online_flag is None else online_flag
            found[p] = discover(p, only)
        db, new = store_results(found)
        STATUS.update(last_run=dt.datetime.now().isoformat(timespec="seconds"), last_new=len(new), total=len(db))
        META_F.write_text(json.dumps(STATUS))
        print(f"Scan done: {len(db)} active, {len(new)} new -> {DATA_DIR}")
    except Exception as e:
        print(f"[scan] failed: {e}")
    finally:
        STATUS["running"] = False
        LOCK.release()
    return True


def scan_background(profiles, online_flag):
    if STATUS["running"]:
        return False
    threading.Thread(target=scan, args=(profiles, online_flag), daemon=True).start()
    return True


# ------------------------------------------------------------------ dashboard server
def make_handler(profiles, online_flag):
    class H(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj):
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            p = urlparse(self.path).path
            if p == "/api/status":
                return self._json(STATUS)
            if p == "/api/run":
                return self._json({"started": scan_background(profiles, online_flag)})
            if p in ("/", "/index.html") or p.startswith("/data/"):
                return super().do_GET()
            self.send_error(404)  # never expose .env or source files

    return functools.partial(H, directory=str(ROOT))


def sleep_until(hhmm):
    h, m = map(int, hhmm.split(":"))
    now = dt.datetime.now()
    nxt = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if nxt <= now:
        nxt += dt.timedelta(days=1)
    time.sleep((nxt - now).total_seconds())


def main():
    ap = argparse.ArgumentParser(description="Competition radar bot")
    ap.add_argument("--profile", choices=[*PROFILES, "all"], default="all")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--online-only", action="store_true", help="drop in-person events for every category")
    g.add_argument("--include-offline", action="store_true", help="keep in-person events for every category")
    ap.add_argument("--serve", action="store_true", help="serve the dashboard")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--daily", metavar="HH:MM", help="scan every day at this local time")
    a = ap.parse_args()
    profiles = list(PROFILES) if a.profile == "all" else [a.profile]
    flag = True if a.online_only else False if a.include_offline else None

    if META_F.exists():
        try:
            STATUS.update(json.loads(META_F.read_text()), running=False)
        except Exception:
            pass

    if not (a.serve or a.daily):
        return scan(profiles, flag)

    if a.serve:
        srv = ThreadingHTTPServer((a.host, a.port), make_handler(profiles, flag))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"Dashboard: http://{a.host}:{a.port}   (Ctrl+C to stop)")
    if not DB_F.exists():
        scan_background(profiles, flag)
    try:
        if a.daily:
            while True:
                sleep_until(a.daily)
                scan(profiles, flag)
        else:
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        print("bye")


if __name__ == "__main__":
    main()
