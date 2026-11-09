#!/usr/bin/env python3
"""Assemble the Jobs table from the feed run and the web-page crawl. Copies fields only."""
import json, os, re, sys, hashlib, collections
sys.path.insert(0, os.path.dirname(__file__))
import jobscrape as J
import ats_probe as A

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
B = os.path.join(ROOT, "data")
F = {r["d"]: r for r in json.load(open(f"{B}/final.json"))}
SRC = json.load(open(f"{B}/sources.json"))
ATS = json.load(open(f"{B}/ats.json")) if os.path.exists(f"{B}/ats.json") else {}
PG = json.load(open(f"{B}/pages.json")) if os.path.exists(f"{B}/pages.json") else {}
ROUTED_PATH = f"{B}/routed.json"
ROUTED = json.load(open(ROUTED_PATH)) if os.path.exists(ROUTED_PATH) else {}

EXTRA = json.load(open(f"{B}/extra.json")) if os.path.exists(f"{B}/extra.json") else {}

def norm(s): return re.sub(r"[^a-z0-9]", "", (s or "").lower())

status = {}   # domain -> {state, jobs, source, reason}
alljobs = []

def add(d, jobs, source):
    for j in jobs:
        j["d"] = d; j["company"] = F[d]["n"]; j["via"] = source
    alljobs.extend(jobs)

for s in SRC:
    d = s["d"]
    if s["kind"] == "linkedin":
        status[d] = dict(state="not fetched", jobs=0, source="LinkedIn", reason="LinkedIn blocks automated reading; not fetched")
        continue
    if s["kind"] in J.READERS:
        r = ATS.get(d)
        if not r:
            status[d] = dict(state="not fetched", jobs=0, source=s["kind"], reason="not run yet"); continue
        if r["status"] == "ok":
            add(d, r["jobs"], r["jobs"][0]["src"] if r["jobs"] else s["kind"])
            status[d] = dict(state="fetched", jobs=len(r["jobs"]), source=s["kind"], reason="" if r["jobs"] else "board is live with 0 open roles")
        else:
            reason = r["error"] or "board did not answer"
            if s["kind"] == "workable": reason = "Workable blocked requests from our server (rate limit); not fetched"
            status[d] = dict(state="not fetched", jobs=0, source=s["kind"], reason=reason)
        continue
    # extra readers (Moka, Rippling, HiBob, HRMOS, custom-domain boards)
    if d in EXTRA:
        e = EXTRA[d]
        if e["status"] == "ok":
            add(d, e["jobs"], (e["jobs"][0]["src"] if e["jobs"] else e["kind"]))
            status[d] = dict(state="fetched", jobs=len(e["jobs"]), source=e["kind"], reason="" if e["jobs"] else "board is live with 0 open roles")
            continue
        if e.get("error") and "404" in e["error"]:
            status[d] = dict(state="not fetched", jobs=0, source=e["kind"], reason=e["error"]); continue
    # web page
    p = PG.get(d)
    if not p:
        status[d] = dict(state="not fetched", jobs=0, source="web page", reason="not crawled yet"); continue
    # 1) an ATS behind the page -> use its feed
    routed = None
    for u in p.get("ats_urls", []):
        a, code = A.code_from(u)
        if a in J.READERS and code and not (a == "greenhouse" and code in ("embed",)):
            routed = (a, code, u); break
    if routed:
        key = f"{routed[0]}:{routed[1]}"
        if key not in ROUTED:
            try: ROUTED[key] = J.READERS[routed[0]](routed[1], routed[2])
            except Exception: ROUTED[key] = None
            json.dump(ROUTED, open(ROUTED_PATH, "w"))
        jobs = ROUTED[key]
        if jobs is not None:
            add(d, jobs, (jobs[0]["src"] if jobs else routed[0]) + " (found behind the careers page)")
            status[d] = dict(state="fetched", jobs=len(jobs), source=routed[0] + " via careers page", reason="" if jobs else "board is live with 0 open roles")
            continue
    # 2) structured JobPosting data published by the page itself
    lds = [(x, x.get("__page") or p["jb"]) for x in p.get("detail_ld", [])] + [(x, p["jb"]) for x in p.get("board_ld", [])]
    jobs, seen, dropped = [], set(), 0
    cn = norm(F[d]["n"])
    for x, page in lds:
        j = J.from_jsonld(x, page, "Job page structured data (schema.org JobPosting)")
        org = norm(j.pop("org", "") or "")
        if org and cn and not (cn in org or org in cn or norm(d.split(".")[0]) in org):
            dropped += 1; continue   # posting names a different employer
        k = j["url"] or j["title"]
        if not j["title"] or k in seen: continue
        seen.add(k); jobs.append(j)
    if jobs:
        add(d, jobs, "Job page structured data")
        status[d] = dict(state="fetched", jobs=len(jobs), source="web page (structured data)", reason=(f"{dropped} postings naming another employer were excluded" if dropped else ""))
    else:
        why = ("page did not finish loading in a browser, and a plain fetch found no machine-readable job data" if "timeout" in (p.get("error") or "") else p.get("error")) or (f"page lists {p.get('candidates',0)} job links but publishes no machine-readable job data" if p.get("candidates") else "no job listings found on the page")
        status[d] = dict(state="not fetched", jobs=0, source="web page", reason=why)

# affiliate jobs found outside the directory (see data/outside_jobs.json); company as stated by the posting
for o in (json.load(open(f"{B}/outside_jobs.json")) if os.path.exists(f"{B}/outside_jobs.json") else []):
    alljobs.append(dict(url=o["url"], title=o["title"], posted=o.get("posted",""), setup=o.get("setup",""), countries=o.get("countries",[]),
                        loc=o.get("loc",""), auth=o.get("auth",""), auth_q=o.get("auth_q",""), salary=o.get("salary") or {}, desc=o.get("desc",""),
                        d=o["d"], company=o["company"], via="Affiliate job search: " + o.get("source","")))

# companies with no job board at all
for d, r in F.items():
    if d not in status:
        status[d] = dict(state="no job board", jobs=0, source="", reason="no job board in the directory")

# ids, de-dup, index + descriptions
idx, descs, seen = [], {}, set()
for j in alljobs:
    key = (j["d"], j["url"] or j["title"])
    if key in seen: continue
    seen.add(key)
    jid = hashlib.md5(("|".join(key)).encode()).hexdigest()[:10]
    s = j.get("salary") or {}
    idx.append(dict(id=jid, u=j["url"], t=j["title"].strip(), c=j["company"], d=j["d"], p=j["posted"], ws=j["setup"],
                    cc=j["countries"], loc=j["loc"], wa=j["auth"], waq=j["auth_q"][:300],
                    sc=s.get("cur", ""), smin=s.get("min"), smax=s.get("max"), sper=s.get("per", ""), sraw=s.get("raw", ""),
                    via=j["via"]))
    descs[jid] = j["desc"]
# one row per posting URL: a board shared by several directory companies lists each job once
byurl, merged = {}, []
for r in idx:
    if r["u"] and r["u"] in byurl:
        first = byurl[r["u"]]
        if r["c"] not in first["c"].split(" / "): first["c"] += " / " + r["c"]
        descs.pop(r["id"], None); continue
    if r["u"]: byurl[r["u"]] = r
    merged.append(r)
idx = merged
idx.sort(key=lambda r: (r["p"] or "0000"), reverse=True)

# chunk descriptions (~1.5 MB each)
outdir = os.path.join(ROOT, "jobs"); os.makedirs(outdir, exist_ok=True)
for f in os.listdir(outdir): os.remove(os.path.join(outdir, f))
chunk, size, n = {}, 0, 0
cmap = {}
for r in idx:
    t = descs.get(r["id"], "")
    chunk[r["id"]] = t; size += len(t.encode()); cmap[r["id"]] = n
    if size > 1_500_000:
        json.dump(chunk, open(f"{outdir}/desc_{n}.json", "w"), ensure_ascii=False, separators=(",", ":")); n += 1; chunk, size = {}, 0
if chunk: json.dump(chunk, open(f"{outdir}/desc_{n}.json", "w"), ensure_ascii=False, separators=(",", ":")); n += 1
for r in idx: r["k"] = cmap[r["id"]]
json.dump(idx, open(f"{B}/jobs_index.json", "w"), ensure_ascii=False, separators=(",", ":"))
json.dump(status, open(f"{B}/jobs_status.json", "w"), ensure_ascii=False, indent=0)
c = collections.Counter(v["state"] for v in status.values())
print("jobs", len(idx), "| desc chunks", n, "|", dict(c))
print("fields filled: posted", sum(1 for r in idx if r["p"]), "setup", sum(1 for r in idx if r["ws"]), "country", sum(1 for r in idx if r["cc"]),
      "auth", sum(1 for r in idx if r["wa"]), "salary", sum(1 for r in idx if r["smin"] or r["smax"] or r["sraw"]), "desc", sum(1 for r in idx if len(descs[r["id"]]) > 100))
