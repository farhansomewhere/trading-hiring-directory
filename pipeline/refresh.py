#!/usr/bin/env python3
"""Daily refresh: re-read every job board, re-apply the affiliate rules, rebuild the site.

Run from the repository root:  python pipeline/refresh.py
Scraper rules: every value is copied as the source states it; nothing is inferred.
A board that fails today keeps its last good result for up to 3 days (marked in the fetch log),
so one flaky night doesn't wipe a company's jobs.
"""
import json, os, re, sys, time, datetime, subprocess, concurrent.futures as cf
from urllib.parse import urljoin, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
D = os.path.join(ROOT, "data")
sys.path.insert(0, HERE)
import jobscrape as J
import ats_probe as A

TODAY = datetime.date.today().isoformat()
load = lambda n, d: json.load(open(os.path.join(D, n))) if os.path.exists(os.path.join(D, n)) else d
save = lambda n, v: json.dump(v, open(os.path.join(D, n), "w"), ensure_ascii=False, separators=(",", ":"))
KEEP_DAYS = 3

def fresh_or_previous(prev, res):
    """Keep yesterday's good result for a few days if today's read failed."""
    if res["status"] == "ok":
        res["fetched"] = TODAY; return res
    if prev and prev.get("status") == "ok":
        age = (datetime.date.fromisoformat(TODAY) - datetime.date.fromisoformat(prev.get("fetched", TODAY))).days
        if age <= KEEP_DAYS:
            prev = dict(prev); prev["stale_note"] = f"board did not answer on {TODAY}; showing jobs last read on {prev.get('fetched')}"; return prev
    return res

def run_readers(items, out_name):
    prev = load(out_name, {})
    out = {}
    def one(s):
        try: jobs = J.READERS[s["kind"]](s["code"], s["jb"])
        except Exception as e: jobs, err = None, f"reader error: {type(e).__name__}"
        else: err = "" if jobs is not None else "board did not answer"
        res = {"kind": s["kind"], "code": s["code"], "jb": s["jb"], "status": "ok" if jobs is not None else "failed", "error": err, "jobs": jobs or []}
        return s["d"], fresh_or_previous(prev.get(s["d"]), res)
    work = [s for s in items if s["kind"] != "workable"]
    with cf.ThreadPoolExecutor(6) as ex:
        for d, r in ex.map(one, work): out[d] = r
    for s in [s for s in items if s["kind"] == "workable"]:   # Workable rate-limits: one at a time
        d, r = one(s); out[d] = r
    save(out_name, out)
    ok = sum(1 for v in out.values() if v["status"] == "ok")
    print(f"{out_name}: {ok}/{len(out)} boards read, {sum(len(v['jobs']) for v in out.values())} jobs", flush=True)

JOBLINK = re.compile(r"(/jobs?/|/careers?/[^/]+|/positions?/|/vacanc|/openings?/|/o/|/p/|/posting|/roles?/|/job-detail|/recruit/|/offer|gh_jid=|/apply/)", re.I)
def refresh_pages():
    """Company careers pages: re-read each page's own structured job data (plain HTTP)."""
    P = load("pages.json", {})
    def one(item):
        d, v = item
        if not (v.get("detail_ld") or v.get("board_ld")): return d, v          # nothing machine-readable before; keep as is
        r = J.get(v["jb"])
        if r is None or r.status_code >= 400: return d, v
        host = urlparse(r.url).hostname or ""; root = ".".join(host.split(".")[-2:])
        links = [urljoin(r.url, m) for m in re.findall(r'href=["\']([^"\'#]+)', r.text)]
        cand = [u for u in dict.fromkeys(links) if (urlparse(u).hostname or "").endswith(root) and JOBLINK.search(urlparse(u).path + "?" + urlparse(u).query) and u != v["jb"]][:120]
        det = []
        for u in cand:
            g = J.get(u)
            if g is not None and g.status_code < 400:
                for x in J.jsonld_jobs(g.text): x["__page"] = u; det.append(x)
        board = J.jsonld_jobs(r.text)
        if not det and not board: return d, v
        nv = dict(v); nv.update(board_ld=board, detail_ld=det, candidates=len(cand), checked=len(cand), error="", fetched=TODAY)
        return d, nv
    with cf.ThreadPoolExecutor(6) as ex:
        for d, v in ex.map(one, list(P.items())): P[d] = v
    save("pages.json", P)
    print(f"pages.json: {sum(1 for v in P.values() if v.get('detail_ld') or v.get('board_ld'))} pages with structured jobs", flush=True)

def recheck_outside():
    """Affiliate jobs found outside the directory: keep only postings that are still open."""
    O = load("outside_jobs.json", [])
    def alive(j):
        try:
            r = J.S.get(j["url"], timeout=25, allow_redirects=True)
            if r.status_code in (404, 410): return False
            if r.status_code < 400 and urlparse(r.url).path.rstrip("/") in ("", "/jobs", "/careers") and urlparse(j["url"]).path.rstrip("/") not in ("", "/jobs", "/careers"): return False
            return True
        except Exception:
            return True   # network trouble is not proof the job closed
    with cf.ThreadPoolExecutor(8) as ex:
        flags = list(ex.map(alive, O))
    kept = [j for j, f in zip(O, flags) if f]
    for j, f in zip(O, flags):
        if not f: print("closed:", j["company"], "|", j["title"])
    save("outside_jobs.json", kept)
    print(f"outside_jobs.json: {len(kept)} open of {len(O)}", flush=True)

def first_seen():
    idx = load("jobs_index.json", [])
    fs = load("first_seen.json", {})
    for j in idx:
        if j["id"] not in fs: fs[j["id"]] = TODAY
        j["fs"] = fs[j["id"]]
    save("first_seen.json", fs); save("jobs_index.json", idx)

def main():
    t = time.time()
    src = load("sources.json", [])
    run_readers([s for s in src if s["kind"] in J.READERS], "ats.json")
    run_readers(load("extra_sources.json", []), "extra.json")
    refresh_pages()
    if os.path.exists(os.path.join(D, "routed.json")): os.remove(os.path.join(D, "routed.json"))   # re-read ATS boards found behind careers pages
    recheck_outside()
    for step in ("jobs_build.py", "aff_rules.py"):
        subprocess.run([sys.executable, os.path.join(HERE, step)], check=True)
    first_seen()
    subprocess.run([sys.executable, os.path.join(HERE, "build_site.py")], check=True)
    print(f"refresh done in {int(time.time()-t)}s", flush=True)

if __name__ == "__main__":
    main()
