#!/usr/bin/env python3
"""Job scraper: reads each company's job board feed and copies fields exactly as stated.
No inference: a field is set only when the feed's own field (or the posting text, for
work authorization) states it explicitly. Otherwise it is left blank."""
import html, json, re, sys, time, os, threading, datetime, concurrent.futures as cf
from urllib.parse import urlparse, quote
import requests, pycountry
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(__file__))
import ats_probe as A

S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
S.verify = os.environ.get("REQUESTS_CA_BUNDLE") or True
T = 30

def get(url, **kw):
    for i in range(3):
        try:
            r = S.get(url, timeout=T, **kw)
            if r.status_code == 429:
                time.sleep(5 * (i + 1)); continue
            return r
        except requests.RequestException:
            time.sleep(2)
    return None

def jget(url, **kw):
    r = get(url, headers={"Accept": "application/json"}, **kw)
    if r is None or r.status_code != 200: return None
    try: return r.json()
    except ValueError: return None

# ---------------------------------------------------------------- text helpers
def text_of(h):
    if not h: return ""
    h = html.unescape(h) if "&lt;" in h else h
    soup = BeautifulSoup(h, "html.parser")
    for br in soup.find_all("br"): br.replace_with("\n")
    for tag in soup.find_all(["p", "div", "li", "h1", "h2", "h3", "h4", "h5", "tr"]):
        tag.insert_before("\n")
        if tag.name == "li": tag.insert(0, "• ")
    t = soup.get_text()
    t = re.sub(r"[ \t ]+", " ", t)
    t = re.sub(r"\n\s*\n\s*\n+", "\n\n", t)
    return "\n".join(l.strip() for l in t.strip().splitlines()).strip()

def iso_date(v):
    """Return YYYY-MM-DD only for an explicit date/timestamp value."""
    if v in (None, "", 0): return ""
    if isinstance(v, (int, float)):
        if v > 1e11: v = v / 1000
        return datetime.datetime.fromtimestamp(v, datetime.timezone.utc).strftime("%Y-%m-%d")
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(v))
    return m.group(1) if m else ""

# ---------------------------------------------------------------- countries
_C = {}
for c in pycountry.countries:
    for n in {c.name, getattr(c, "common_name", None), getattr(c, "official_name", None)}:
        if n: _C[n.lower()] = c.alpha_2
ALIAS = {"uk": "GB", "u.k.": "GB", "england": "GB", "scotland": "GB", "wales": "GB", "northern ireland": "GB",
         "usa": "US", "u.s.a.": "US", "u.s.": "US", "united states of america": "US", "uae": "AE", "u.a.e.": "AE",
         "russia": "RU", "south korea": "KR", "korea": "KR", "vietnam": "VN", "turkey": "TR", "türkiye": "TR",
         "czech republic": "CZ", "czechia": "CZ", "taiwan": "TW", "hong kong": "HK", "macau": "MO", "macao": "MO",
         "iran": "IR", "syria": "SY", "laos": "LA", "moldova": "MD", "bolivia": "BO", "venezuela": "VE",
         "tanzania": "TZ", "the netherlands": "NL", "holland": "NL", "ivory coast": "CI", "cape verde": "CV",
         "brunei": "BN", "palestine": "PS", "kosovo": "XK", "eswatini": "SZ", "mainland china": "CN"}
_C.update(ALIAS)
_CNAMES = sorted(_C, key=len, reverse=True)
_CRE = re.compile(r"(?<![a-z])(" + "|".join(re.escape(n) for n in _CNAMES) + r")(?![a-z])", re.I)

def countries_in(s):
    """Country codes for country names written in the string. Cities alone give nothing."""
    out = []
    for m in _CRE.finditer(s or ""):
        cc = _C.get(m.group(1).lower())
        if cc and cc not in out: out.append(cc)
    # 'US'/'UK' as standalone uppercase tokens (e.g. "New York, NY, US")
    for tok, cc in (("US", "US"), ("UK", "GB"), ("UAE", "AE")):
        if re.search(r"(?<![A-Za-z])" + tok + r"(?![A-Za-z])", s or "") and cc not in out: out.append(cc)
    return out

def cc_valid(code):
    if not code: return ""
    code = str(code).strip().upper()
    if code == "UK": code = "GB"
    if len(code) == 2 and pycountry.countries.get(alpha_2=code): return code
    if len(code) == 3:
        c = pycountry.countries.get(alpha_3=code)
        if c: return c.alpha_2
    c = _C.get(str(code).lower())
    return c or ""

# ---------------------------------------------------------------- work setup
def setup_from_text(*parts):
    """Only explicit words in location/title/workplace fields."""
    s = " ".join(p for p in parts if p).lower()
    found = []
    if re.search(r"\bhybrid\b", s): found.append("Hybrid")
    if re.search(r"\bremote\b|\bwork from home\b|\bwfh\b|\btelecommut", s): found.append("Remote")
    if re.search(r"\bon[- ]?site\b|\bin[- ]office\b|\bin office\b", s): found.append("Onsite")
    if len(found) == 1: return found[0]
    if "Hybrid" in found: return "Hybrid"
    return ""

def setup_norm(v):
    v = (v or "").strip().lower().replace("_", "").replace("-", "").replace(" ", "")
    return {"remote": "Remote", "hybrid": "Hybrid", "onsite": "Onsite", "inoffice": "Onsite", "office": "Onsite",
            "telecommute": "Remote", "fullyremote": "Remote"}.get(v, "")

# ---------------------------------------------------------------- work authorization (explicit phrases)
NEG = r"(?:no|not|unable|cannot|can't|can not|don't|do not|does not|doesn't|won't|will not|without|n't)"
SENT = re.compile(r"[^.\n!?]*[.\n!?]")
def work_auth(desc):
    if not desc: return "", ""
    for s in SENT.findall(desc + "\n"):
        sl = s.lower()
        if re.search(r"(visa|work permit|sponsor)", sl) and re.search(r"\b" + NEG + r"\b[^.]{0,60}(sponsor|visa|work permit)", sl):
            return "Required", s.strip()
    for s in SENT.findall(desc + "\n"):
        sl = s.lower()
        if re.search(r"(must|required|need|should|have to|eligible)[^.]{0,80}(right to work|authori[sz]ed to work|work authori[sz]ation|valid work permit|work permit|eligib\w* to work|legally (?:able|entitled|eligible) to work)", sl) \
           or re.search(r"(right to work|authori[sz]ed to work|work authori[sz]ation|valid work permit|eligib\w* to work|legally (?:able|entitled|eligible) to work)[^.]{0,60}(is )?(required|mandatory|essential|needed)", sl):
            return "Required", s.strip()
    for s in SENT.findall(desc + "\n"):
        sl = s.lower()
        if re.search(r"(work permit|work authori[sz]ation|visa)[^.]{0,30}(is )?not (required|needed|necessary)", sl):
            return "Not Required", s.strip()
    for s in SENT.findall(desc + "\n"):
        sl = s.lower()
        if re.search(r"visa sponsorship|sponsor (?:your |a |the )?(?:work )?visa|visa support|relocation (?:support|package|assistance|bonus|allowance)|relocation and visa|visa and relocation|help (?:you )?(?:with )?relocat", sl):
            return "Visa & Relocation Support", s.strip()
    return "", ""

# ---------------------------------------------------------------- salary (structured only)
def sal(cur=None, mn=None, mx=None, period=None):
    def num(x):
        if x in (None, "", 0): return None
        try: return float(str(x).replace(",", ""))
        except ValueError: return None
    mn, mx = num(mn), num(mx)
    if mn is None and mx is None: return {}
    p = (period or "").lower()
    p = "a year" if re.search(r"year|annual|annum|yr", p) else "a month" if "month" in p else "an hour" if "hour" in p else "a week" if "week" in p else "a day" if "day" in p else (period or "")
    return {"cur": (cur or "").upper(), "min": mn, "max": mx, "per": p}

def job(**k):
    d = dict(url="", title="", posted="", setup="", countries=[], loc="", auth="", auth_q="", salary={}, desc="", src="")
    d.update(k)
    if not d["auth"]:
        d["auth"], d["auth_q"] = work_auth(d["desc"])
    return d

# ---------------------------------------------------------------- readers
def r_greenhouse(code, board_url):
    host = "boards-api.greenhouse.io"  # also serves EU-hosted boards (the .eu API host does not resolve)
    data = jget(f"https://{host}/v1/boards/{code}/jobs?content=true")
    if not data: return None
    out = []
    def one(j):
        pay = {}
        det = jget(f"https://{host}/v1/boards/{code}/jobs/{j['id']}?pay_transparency=true") or {}
        for pr in det.get("pay_input_ranges") or []:
            pay = sal(pr.get("currency_type"), (pr.get("min_cents") or 0) / 100 or None, (pr.get("max_cents") or 0) / 100 or None, pr.get("title") or "")
            if pay and not pay["per"]: pay["per"] = ""
            break
        loc = (j.get("location") or {}).get("name", "")
        offices = ", ".join(o.get("location") or o.get("name", "") for o in (det.get("offices") or []) if o)
        cs = countries_in(loc) or countries_in(offices)
        return job(url=j.get("absolute_url", ""), title=j.get("title", ""), posted=iso_date(j.get("first_published")),
                   setup=setup_from_text(loc, j.get("title", "")), countries=cs, loc=loc,
                   salary=pay, desc=text_of(j.get("content")), src="Greenhouse API")
    with cf.ThreadPoolExecutor(4) as ex:
        out = list(ex.map(one, data.get("jobs", [])))
    return out

def r_lever(code, board_url):
    for api in (("api.eu.lever.co",) if ".eu." in board_url else ("api.lever.co", "api.eu.lever.co")):
        data = jget(f"https://{api}/v0/postings/{code}?mode=json")
        if isinstance(data, list): break
    else:
        return None
    out = []
    for p in data:
        cat = p.get("categories") or {}
        loc = cat.get("location", "") or ""
        allloc = ", ".join(cat.get("allLocations") or [])
        desc = (p.get("descriptionPlain") or text_of(p.get("description"))) + "\n\n"
        for l in p.get("lists") or []:
            desc += (l.get("text") or "") + "\n" + text_of(l.get("content")) + "\n\n"
        desc += p.get("additionalPlain") or text_of(p.get("additional"))
        sr = p.get("salaryRange") or {}
        cs = [cc_valid(p.get("country"))] if cc_valid(p.get("country")) else (countries_in(loc + " " + allloc))
        out.append(job(url=p.get("hostedUrl", ""), title=p.get("text", ""), posted=iso_date(p.get("createdAt")),
                       setup=setup_norm(p.get("workplaceType")) or setup_from_text(loc, p.get("text", "")),
                       countries=cs, loc=allloc or loc,
                       salary=sal(sr.get("currency"), sr.get("min"), sr.get("max"), sr.get("interval")),
                       desc=desc.strip(), src="Lever API"))
    return out

def r_ashby(code, board_url):
    data = jget(f"https://api.ashbyhq.com/posting-api/job-board/{quote(code)}?includeCompensation=true")
    if not data or "jobs" not in data: return None
    out = []
    for j in data["jobs"]:
        if j.get("isListed") is False: continue
        locs = [j.get("location") or ""] + [x.get("location", "") for x in (j.get("secondaryLocations") or [])]
        cs = []
        addr = ((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry")
        if cc_valid(addr): cs.append(cc_valid(addr))
        for x in j.get("secondaryLocations") or []:
            a2 = cc_valid(((x.get("address") or {}).get("addressCountry")))
            if a2 and a2 not in cs: cs.append(a2)
        if not cs: cs = countries_in(" ".join(locs))
        pay = {}
        for c in ((j.get("compensation") or {}).get("summaryComponents") or []):
            if (c.get("compensationType") or "").lower() == "salary":
                pay = sal(c.get("currencyCode"), c.get("minValue"), c.get("maxValue"), c.get("interval")); break
        st = setup_norm(j.get("workplaceType")) or ("Remote" if j.get("isRemote") is True and not j.get("workplaceType") else "")
        out.append(job(url=j.get("jobUrl", ""), title=j.get("title", ""), posted=iso_date(j.get("publishedAt")),
                       setup=st, countries=cs, loc=", ".join(l for l in locs if l), salary=pay,
                       desc=j.get("descriptionPlain") or text_of(j.get("descriptionHtml")), src="Ashby API"))
    return out

def r_smartrecruiters(code, board_url):
    out, off = [], 0
    while True:
        d = jget(f"https://api.smartrecruiters.com/v1/companies/{code}/postings?limit=100&offset={off}")
        if not d: return None if off == 0 else out
        items = d.get("content", [])
        for p in items:
            det = jget(f"https://api.smartrecruiters.com/v1/companies/{code}/postings/{p['id']}") or {}
            loc = p.get("location") or {}
            secs = ((det.get("jobAd") or {}).get("sections") or {})
            desc = "\n\n".join((v.get("title", "") + "\n" + text_of(v.get("text", ""))).strip() for v in secs.values() if isinstance(v, dict) and v.get("text"))
            st = "Remote" if loc.get("remote") is True else ("Hybrid" if loc.get("hybrid") is True else "")
            out.append(job(url=det.get("postingUrl") or f"https://jobs.smartrecruiters.com/{code}/{p['id']}", title=p.get("name", ""),
                           posted=iso_date(p.get("releasedDate")), setup=st, countries=[cc_valid(loc.get("country"))] if cc_valid(loc.get("country")) else [],
                           loc=loc.get("fullLocation") or ", ".join(x for x in (loc.get("city"), loc.get("country")) if x),
                           desc=desc, src="SmartRecruiters API"))
        off += 100
        if off >= d.get("totalFound", 0) or not items: break
    return out

def r_bamboohr(code, board_url):
    d = jget(f"https://{code}.bamboohr.com/careers/list")
    if not isinstance(d, dict): return None
    out = []
    for j in d.get("result", []):
        det = (jget(f"https://{code}.bamboohr.com/careers/{j['id']}/detail") or {}).get("result", {}).get("jobOpening", {})
        loc = j.get("location") or {}
        al = j.get("atsLocation") or det.get("atsLocation") or {}
        dl = det.get("location") or {}
        country = dl.get("addressCountry") or al.get("country") or ""
        locs = ", ".join(x for x in (loc.get("city"), loc.get("state"), country) if x)
        cs = [cc_valid(country)] if cc_valid(country) else countries_in(locs)
        lt = str(det.get("locationType", j.get("locationType", "")))
        st = {"1": "Remote", "2": "Hybrid"}.get(lt, "")  # BambooHR shows no label for type 0, so it stays blank
        if not st and j.get("isRemote") is True: st = "Remote"
        comp = det.get("compensation") or ""
        out.append(job(url=det.get("jobOpeningShareUrl") or f"https://{code}.bamboohr.com/careers/{j['id']}",
                       title=j.get("jobOpeningName", ""), posted=iso_date(det.get("datePosted")), setup=st, countries=cs, loc=locs,
                       desc=text_of(det.get("description")), src="BambooHR API",
                       salary={"raw": comp} if comp else {}))
    return out

def r_teamtailor(code, board_url):
    host = urlparse(board_url).hostname if board_url else f"{code}.teamtailor.com"
    d = jget(f"https://{host}/jobs.json") or jget(f"https://{code}.teamtailor.com/jobs.json")
    if not d or "items" not in d: return None
    out = []
    for i in d["items"]:
        jp = i.get("_jobposting") or {}
        locs = jp.get("jobLocation") or []
        if isinstance(locs, dict): locs = [locs]
        cs = []
        for l in locs:
            a = cc_valid(((l or {}).get("address") or {}).get("addressCountry"))
            if a and a not in cs: cs.append(a)
        st = "Remote" if jp.get("jobLocationType") == "TELECOMMUTE" else ""
        bs = (jp.get("baseSalary") or {})
        v = bs.get("value") or {}
        out.append(job(url=i.get("url", ""), title=i.get("title", ""), posted=iso_date(i.get("date_published") or jp.get("datePosted")),
                       setup=st, countries=cs, loc=", ".join(((l or {}).get("address") or {}).get("addressLocality", "") for l in locs if l),
                       salary=sal(bs.get("currency"), v.get("minValue") or v.get("value"), v.get("maxValue"), v.get("unitText")) if v else {},
                       desc=text_of(i.get("content_html")), src="Teamtailor feed"))
    return out

def r_recruitee(code, board_url):
    d = jget(f"https://{code}.recruitee.com/api/offers/")
    if not d or "offers" not in d: return None
    out = []
    for o in d["offers"]:
        st = "Remote" if o.get("remote") else "Hybrid" if o.get("hybrid") else "Onsite" if o.get("on_site") else ""
        s = o.get("salary") or {}
        out.append(job(url=o.get("careers_url", ""), title=o.get("title", ""), posted=iso_date(o.get("published_at")), setup=st,
                       countries=[cc_valid(o.get("country_code"))] if cc_valid(o.get("country_code")) else countries_in(o.get("location", "")),
                       loc=o.get("location", ""), salary=sal(s.get("currency"), s.get("min"), s.get("max"), s.get("period")),
                       desc=text_of((o.get("description") or "") + "<br>" + (o.get("requirements") or "")), src="Recruitee API"))
    return out

def jsonld_jobs(html_text):
    out = []
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html_text or "", re.S | re.I):
        try: data = json.loads(m.group(1).strip())
        except Exception: continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                if x.get("@type") == "JobPosting" or (isinstance(x.get("@type"), list) and "JobPosting" in x["@type"]): out.append(x)
                for k in ("@graph", "itemListElement", "item"):
                    v = x.get(k)
                    if isinstance(v, list): stack.extend(v)
                    elif isinstance(v, dict): stack.append(v)
            elif isinstance(x, list): stack.extend(x)
    return out

def from_jsonld(jp, url, src):
    locs = jp.get("jobLocation") or []
    if isinstance(locs, dict): locs = [locs]
    cs, names = [], []
    for l in locs:
        a = (l or {}).get("address") or {}
        if isinstance(a, str): names.append(a); continue
        c = a.get("addressCountry")
        if isinstance(c, dict): c = c.get("name") or c.get("@id")
        cc = cc_valid(c)
        if cc and cc not in cs: cs.append(cc)
        names.append(", ".join(str(a.get(k)) for k in ("addressLocality", "addressRegion", "addressCountry") if a.get(k) and isinstance(a.get(k), str)))
    st = "Remote" if jp.get("jobLocationType") == "TELECOMMUTE" else ""
    bs = jp.get("baseSalary") or {}
    v = bs.get("value") if isinstance(bs, dict) else None
    pay = {}
    if isinstance(v, dict): pay = sal(bs.get("currency"), v.get("minValue") or v.get("value"), v.get("maxValue"), v.get("unitText"))
    hp = jp.get("hiringOrganization") or {}
    return job(url=jp.get("url") or url, title=html.unescape(jp.get("title") or ""), posted=iso_date(jp.get("datePosted")), setup=st,
               countries=cs, loc="; ".join(n for n in names if n), salary=pay, desc=text_of(jp.get("description") or ""),
               src=src, org=(hp.get("name") if isinstance(hp, dict) else ""))

def r_breezy(code, board_url):
    r = get(f"https://{code}.breezy.hr/json", headers={"Accept": "application/json"})
    if r is None or r.status_code != 200: return None
    try: data = r.json()
    except ValueError: return None
    out = []
    for p in data:
        pg = get(p.get("url", ""))
        jl = jsonld_jobs(pg.text if pg is not None else "")
        if jl:
            j = from_jsonld(jl[0], p.get("url", ""), "Breezy board + job page data")
        else:
            loc = p.get("location") or {}
            j = job(url=p.get("url", ""), title=p.get("name", ""), loc=(loc.get("name") or ""), src="Breezy board")
        if not j["posted"]: j["posted"] = iso_date(p.get("published_date"))
        loc = p.get("location") or {}
        if not j["countries"]:
            c = cc_valid(((loc.get("country") or {}).get("id")))
            if c: j["countries"] = [c]
        if not j["setup"] and loc.get("is_remote") is True: j["setup"] = "Remote"
        out.append(j)
    return out

def r_comeet(code, board_url):
    if "|" not in code: return None
    slug, uid = code.split("|", 1)
    page = get(f"https://www.comeet.com/jobs/{slug}/{uid}")
    if page is None: return None
    m = re.search(r'"token"\s*:\s*"([A-Za-z0-9]{16,})"', page.text)
    if not m: return None
    data = jget(f"https://www.comeet.com/careers-api/2.0/company/{uid}/positions?token={m.group(1)}&details=true")
    if not isinstance(data, list): return None
    out = []
    for p in data:
        loc = p.get("location") or {}
        desc = "\n\n".join(((d.get("name") or "") + "\n" + text_of(d.get("value") or "")).strip() for d in (p.get("details") or []))
        st = setup_norm(p.get("workplace_type")) or ("Remote" if loc.get("is_remote") is True else "")
        out.append(job(url=p.get("url_active_page") or p.get("url_comeet_hosted_page") or "", title=p.get("name", ""),
                       setup=st, countries=[cc_valid(loc.get("country"))] if cc_valid(loc.get("country")) else [],
                       loc=", ".join(x for x in (loc.get("city"), loc.get("country")) if x), desc=desc, src="Comeet API"))
    return out

def r_pinpoint(code, board_url):
    d = jget(f"https://{code}.pinpointhq.com/postings.json")
    if d is None: return None
    rows = d.get("data", []) if isinstance(d, dict) else d
    out = []
    for p in rows:
        a = p.get("attributes", p)
        url = a.get("url") or p.get("url") or ""
        pg = get(url) if url else None
        jl = jsonld_jobs(pg.text if pg is not None else "")
        if jl: out.append(from_jsonld(jl[0], url, "Pinpoint board + job page data"))
        else: out.append(job(url=url, title=a.get("title", ""), loc=(a.get("location") or {}).get("name", "") if isinstance(a.get("location"), dict) else "",
                             setup=setup_norm(a.get("workplace_type")), desc=text_of(a.get("description") or ""), src="Pinpoint board"))
    return out

def r_workday(code, board_url):
    tenant, wd, site = code.split("|")
    base = f"https://{tenant}.{wd}.myworkdayjobs.com"
    out, off, total = [], 0, None
    while True:
        try:
            r = S.post(f"{base}/wday/cxs/{tenant}/{site}/jobs", json={"appliedFacets": {}, "limit": 20, "offset": off, "searchText": ""}, timeout=T)
        except requests.RequestException:
            r = None
        if r is None or r.status_code != 200: return None if off == 0 else out
        d = r.json()
        if total is None: total = d.get("total", 0)
        posts = d.get("jobPostings", [])
        for p in posts:
            det = (jget(f"{base}/wday/cxs/{tenant}/{site}{p.get('externalPath','')}") or {}).get("jobPostingInfo", {})
            cc = cc_valid(((det.get("country") or {}).get("alpha2Code"))) or ""
            cs = [cc] if cc else countries_in(det.get("location") or p.get("locationsText") or "")
            out.append(job(url=det.get("externalUrl") or f"{base}/{site}{p.get('externalPath','')}", title=det.get("title") or p.get("title", ""),
                           posted=iso_date(det.get("startDate")), setup=setup_norm(det.get("remoteType")) or setup_from_text(det.get("remoteType", "")),
                           countries=cs, loc=det.get("location") or p.get("locationsText", ""), desc=text_of(det.get("jobDescription")),
                           src="Workday API"))
        off += 20
        if off >= (total or 0) or not posts: break
    return out

def r_workable(code, board_url):
    time.sleep(6)
    d = jget(f"https://apply.workable.com/api/v1/widget/accounts/{code}?details=true")
    if not d or "jobs" not in d: return None
    out = []
    for j in d["jobs"]:
        locs = j.get("locations") or [{"country": j.get("country"), "city": j.get("city"), "countryCode": j.get("country_code")}]
        cs = []
        for l in locs:
            c = cc_valid(l.get("countryCode")) or cc_valid(l.get("country"))
            if c and c not in cs: cs.append(c)
        st = "Remote" if j.get("telecommuting") is True else ""
        out.append(job(url=j.get("url") or j.get("application_url", ""), title=j.get("title", ""), posted=iso_date(j.get("published_on") or j.get("created_at")),
                       setup=st, countries=cs, loc="; ".join(", ".join(x for x in (l.get("city"), l.get("country")) if x) for l in locs),
                       desc=text_of(j.get("description") or ""), src="Workable API"))
    return out

READERS = {"greenhouse": r_greenhouse, "lever": r_lever, "ashby": r_ashby, "smartrecruiters": r_smartrecruiters,
           "bamboohr": r_bamboohr, "teamtailor": r_teamtailor, "recruitee": r_recruitee, "breezy": r_breezy,
           "comeet": r_comeet, "pinpoint": r_pinpoint, "workday": r_workday, "workable": r_workable}

def run(sources_path, out_path, kinds=None):
    src = json.load(open(sources_path))
    done = json.load(open(out_path)) if os.path.exists(out_path) else {}
    lock = threading.Lock()
    todo = [s for s in src if s["kind"] in READERS and (not kinds or s["kind"] in kinds) and s["d"] not in done]
    def one(s):
        try: jobs = READERS[s["kind"]](s["code"], s["jb"])
        except Exception as e: jobs, err = None, f"reader error: {type(e).__name__}: {str(e)[:80]}"
        else: err = "" if jobs is not None else "board did not answer"
        res = {"kind": s["kind"], "code": s["code"], "jb": s["jb"], "status": "ok" if jobs is not None else "failed", "error": err, "jobs": jobs or []}
        with lock:
            done[s["d"]] = res
            json.dump(done, open(out_path, "w"))
        print(f"{s['d']:28} {s['kind']:15} {res['status']:6} {len(res['jobs'])} {err}", flush=True)
    workable = [s for s in todo if s["kind"] == "workable"]
    rest = [s for s in todo if s["kind"] != "workable"]
    with cf.ThreadPoolExecutor(6) as ex:
        list(ex.map(one, rest))
    for s in workable: one(s)

if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2], sys.argv[3].split(",") if len(sys.argv) > 3 else None)

# ---------------------------------------------------------------- extra readers (smaller ATSs)
def _moka_desc(host, org, site, jid):
    try:
        d = S.post(f"https://{host}/api/outer/ats-apply/website/job", json={"orgId": org, "siteId": int(site), "jobId": jid}, timeout=T).json()
        return (d.get("data") or {}).get("jobDescription") or ""
    except Exception: return ""

def r_moka(code, board_url):
    org, site = code.split("|")
    host = urlparse(board_url).hostname or "hire-r1.mokahr.com"
    out, off = [], 0
    while True:
        try: d = S.post(f"https://{host}/api/outer/ats-apply/website/jobs/v2", json={"orgId": org, "siteId": int(site), "limit": 50, "offset": off, "needStat": True}, timeout=T).json()
        except Exception: return None if off == 0 else out
        jobs = ((d.get("data") or {}).get("jobs")) or []
        for j in jobs:
            locs = j.get("locations") or []
            cs = []
            for l in locs:
                c = cc_valid(l.get("country"))
                if c and c not in cs: cs.append(c)
            out.append(job(url=f"https://{host}/social-recruitment/{org}/{site}#/job/{j['id']}", title=j.get("title", ""),
                           posted=iso_date(j.get("publishedAt")), countries=cs,
                           loc=", ".join(x for x in (", ".join(filter(None, (l.get("address"), l.get("country")))) for l in locs) if x),
                           desc=text_of(j.get("jobDescription") or _moka_desc(host, org, site, j["id"])), src="Moka API"))
        off += 50
        if off >= ((d.get("data") or {}).get("jobStats") or {}).get("total", 0) or not jobs: break
    return out

def r_rippling(code, board_url):
    lst = jget(f"https://api.rippling.com/platform/api/ats/v1/board/{code}/jobs")
    if not isinstance(lst, list): return None
    out = []
    for j in lst:
        det = jget(f"https://api.rippling.com/platform/api/ats/v1/board/{code}/jobs/{j['uuid']}") or {}
        ld = det.get("jsonLd")
        if isinstance(ld, str):
            try: ld = json.loads(ld)
            except Exception: ld = None
        if isinstance(ld, dict):
            x = from_jsonld(ld, j.get("url", ""), "Rippling API"); x.pop("org", None)
        else:
            x = job(url=j.get("url", ""), title=j.get("name", "").strip(), loc=(j.get("workLocation") or {}).get("label", ""), src="Rippling API")
        if not x["desc"]: x["desc"] = text_of(((det.get("description") or {}).get("role") if isinstance(det.get("description"), dict) else det.get("description")) or "")
        if not x["posted"]: x["posted"] = iso_date(det.get("createdOn"))
        x["title"] = x["title"].strip() or j.get("name", "").strip()
        out.append(x)
    return out

def r_hibob(code, board_url):
    try: d = S.get(f"https://{code}.careers.hibob.com/api/job-ad", headers={"companyidentifier": code}, timeout=T).json()
    except Exception: return None
    out = []
    for j in d.get("jobAdDetails") or []:
        desc = "\n\n".join(text_of(j.get(k) or "") for k in ("description", "responsibilities", "requirements", "benefits") if j.get(k))
        pay = sal(j.get("payTransparencySalaryCurrency"), j.get("payTransparencyMinSalary"), j.get("payTransparencyMaxSalary"), j.get("payTransparencySalaryPayPeriod"))
        out.append(job(url=f"https://{code}.careers.hibob.com/jobs/{j['id']}", title=j.get("title", ""), posted=iso_date(j.get("publishedAt")),
                       setup=setup_norm(j.get("workspaceType")), countries=[cc_valid(j.get("country"))] if cc_valid(j.get("country")) else [],
                       loc=", ".join(x for x in (j.get("site"), j.get("country")) if x), salary=pay, desc=desc, src="HiBob API"))
    return out

def r_hrmos(code, board_url):
    r = get(f"https://hrmos.co/pages/{code}/jobs")
    if r is None or r.status_code != 200: return None
    links = sorted(set(re.findall(r'href="(https://hrmos\.co/pages/' + re.escape(code) + r'/jobs/[0-9A-Za-z]+)"', r.text)))
    out = []
    for u in links:
        g = get(u)
        for x in jsonld_jobs(g.text if g is not None else ""):
            j = from_jsonld(x, u, "HRMOS job page structured data"); j.pop("org", None); j["url"] = u; out.append(j); break
    return out

def r_recruitee_host(code, board_url):
    host = urlparse(board_url).hostname
    d = jget(f"https://{host}/api/offers/")
    if not d or "offers" not in d: return None
    return r_recruitee.__wrapped__(d) if hasattr(r_recruitee, "__wrapped__") else _recruitee_rows(d)

def _recruitee_rows(d):
    out = []
    for o in d["offers"]:
        st = "Remote" if o.get("remote") else "Hybrid" if o.get("hybrid") else "Onsite" if o.get("on_site") else ""
        s = o.get("salary") or {}
        out.append(job(url=o.get("careers_url", ""), title=o.get("title", ""), posted=iso_date(o.get("published_at")), setup=st,
                       countries=[cc_valid(o.get("country_code"))] if cc_valid(o.get("country_code")) else countries_in(o.get("location", "")),
                       loc=o.get("location", ""), salary=sal(s.get("currency"), s.get("min"), s.get("max"), s.get("period")),
                       desc=text_of((o.get("description") or "") + "<br>" + (o.get("requirements") or "")), src="Recruitee API"))
    return out

READERS.update({"moka": r_moka, "rippling": r_rippling, "hibob": r_hibob, "hrmos": r_hrmos, "recruitee_host": r_recruitee_host})

def r_peopleforce(code, board_url):
    """PeopleForce careers template. Title from the job page's og:title ('Company - Title') or its <h1>;
    'Department · Location' line under the title; description is the page body after that line."""
    host = urlparse(board_url).hostname if board_url and "peopleforce" in board_url else f"{code}.peopleforce.io"
    r = get(f"https://{host}/careers")
    if r is None or r.status_code != 200: return None
    links = sorted(set(re.findall(r'href="(/careers/v/[^"?#]+)"', r.text)))
    out = []
    for path in links:
        g = get(f"https://{host}{path}")
        if g is None or g.status_code != 200: continue
        soup = BeautifulSoup(g.text, "html.parser")
        og = soup.find("meta", attrs={"property": "og:title"})
        title = (og.get("content") or "").split(" - ", 1)[-1].strip() if og else ""
        h1 = None
        for h in soup.find_all("h1"):
            if not title or h.get_text(" ", strip=True) == title: h1 = h; break
        if h1 is None: h1 = soup.find("h1")
        if not title and h1 is not None: title = h1.get_text(" ", strip=True)
        if not title: continue
        meta = ""
        nxt = h1.find_next_sibling() if h1 is not None else None
        if nxt is not None: meta = nxt.get_text(" ", strip=True)
        loc = meta.split("·", 1)[1].strip() if "·" in meta else ""
        body = h1.find_parent("div").find_parent("div") if h1 is not None and h1.find_parent("div") is not None else None
        desc = ""
        if body is not None:
            for b in body.find_all(["form", "button", "nav", "script", "style"]): b.decompose()
            if nxt is not None: nxt.decompose()
            h1.decompose()
            desc = text_of(str(body))
            desc = re.sub(r"^(?:Job openings\s*)", "", desc).strip()
        out.append(job(url=f"https://{host}{path}", title=title, loc=loc, countries=countries_in(loc),
                       setup=setup_from_text(loc), desc=desc, src="PeopleForce careers page"))
    return out

def r_herp(code, board_url):
    r = get(f"https://herp.careers/v1/{code}")
    if r is None or r.status_code != 200: return None
    links = sorted(set(re.findall(r'href="(/v1/' + re.escape(code) + r'/[0-9a-f-]{20,})"', r.text)))
    out = []
    for path in links:
        g = get("https://herp.careers" + path)
        for x in jsonld_jobs(g.text if g is not None else ""):
            j = from_jsonld(x, "https://herp.careers" + path, "HERP job page structured data"); j.pop("org", None)
            j["url"] = "https://herp.careers" + path; out.append(j); break
    return out

READERS.update({"peopleforce": r_peopleforce, "herp": r_herp})
