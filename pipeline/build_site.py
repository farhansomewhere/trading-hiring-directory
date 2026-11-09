import json,os,html,collections,pycountry,datetime,sys
ROOT=os.path.abspath(os.path.join(os.path.dirname(__file__),'..'));B=os.path.join(ROOT,'data');sys.path.insert(0,os.path.dirname(__file__))
F=json.load(open(f'{B}/final.json'));J=json.load(open(f'{B}/jobs_index.json'));S=json.load(open(f'{B}/jobs_status.json'))
import aff_rules as R
CN={c.alpha_2:getattr(c,'common_name',None) or c.name for c in pycountry.countries}
CFG={"api":os.environ.get("SHEET_API","https://script.google.com/macros/s/AKfycby9hMfBJYHPMJ6I5bDYQGzZ8VEZ5TCv-wSm5inVfp2-izw3CRw6SSWyy4FttWu4CE_a/exec"),"people":["Farhan","Marjan","Owais"],"th":0.7,"seed":"2026-11-09","rej":[w.replace(" ","\\s+") for w in R.LANG+R.OTHER]}
e=html.escape
inc="".join(f"<li><b>{e(l)}</b></li>" for l,_ in R.INCLUDE)
fetched=sum(1 for v in S.values() if v['state']=='fetched');nf=[(F_[0],v) for F_ in [(d,) for d in S] for v in [S[F_[0]]] if v['state']=='not fetched']
names={r['d']:r['n'] for r in F}
nf_rows="".join(f"<tr><td>{e(names.get(d,d))}</td><td>{e(v['reason'])}</td></tr>" for d,v in sorted(nf,key=lambda x:names.get(x[0],x[0]).lower()))
about=f"""
<div><h2>How Affiliate Jobs are found</h2><p>Every job on {fetched} company job boards is checked. A job enters the Affiliate list when its title matches one of these patterns, unless a reject rule drops it. Your team's Relevant / Not relevant decisions then override the rules and train the Suggested list.</p></div>
<table><tbody>
<tr><td>Title patterns</td><td><ul style="margin:0;padding-left:18px">{inc}</ul><p style="margin-top:6px">Matching is case-insensitive. "Partner" counts only in phrases like Partner Manager, Partner Growth, Partner Marketing or Partner Acquisition, so HR/Finance "Business Partner" roles are not matched.</p></td></tr>
<tr><td>Reject words (title)</td><td>{e(", ".join(R.LANG))}; {e(", ".join(R.OTHER))}</td></tr>
<tr><td>Non-English postings</td><td>Rejected when the description is clearly not written in English.</td></tr>
<tr><td>Team decisions</td><td>✓ Relevant adds a job to the list; ✕ Not relevant removes it. Companies can be added or removed on the Companies tab; a removed company's jobs leave every list. All decisions are shared, override the rules, and are recorded in the Activity log (who, what, which item, when, why). Click a pressed button again to clear it.</td></tr>
<tr><td>Learning</td><td>A small model scores every job from its title words and description signals (e.g. IB, introducing broker, CPA, revenue share, affiliate network), trained on your decisions. Jobs it rates 70%+ that aren't in the list appear under Suggested, with the words that drove the score. The title rules are also reviewed against your decisions when the site is refreshed.</td></tr>
</tbody></table>
<div><h2>Data rules</h2></div>
<table><tbody>
<tr><td>Accuracy</td><td>Scraper only: every value is copied as the job board or posting states it. Nothing is inferred; blank means not stated.</td></tr>
<tr><td>Sources</td><td>ATS feeds (Greenhouse, Lever, Ashby, Workable, BambooHR, SmartRecruiters, Workday, Comeet, Moka, HiBob and others) and the structured job data published by company careers pages. LinkedIn is not fetched.</td></tr>
<tr><td>Work Setup, Country, Work Authorization</td><td>Set only when the board's fields or the posting text state them explicitly. Hover a Work Authorization value to read the exact sentence.</td></tr>
<tr><td>Salary</td><td>As posted: currency, amount or range, and period.</td></tr>
<tr><td>Refresh</td><td>One-time fetch on {datetime.date.today():%d %b %Y}; refreshed on request.</td></tr>
</tbody></table>
<details><summary style="cursor:pointer;font-weight:600">Job boards not fetched ({len(nf)}) and why</summary><table><tbody>{nf_rows}</tbody></table></details>
"""
t=open(os.path.join(os.path.dirname(__file__),'site_template.html')).read()
data='const DATA='+json.dumps(F,ensure_ascii=False,separators=(',',':'))+';const JOBS='+json.dumps(J,ensure_ascii=False,separators=(',',':'))+';const STATUS='+json.dumps(S,ensure_ascii=False,separators=(',',':'))+';const COUNTRIES='+json.dumps(CN,ensure_ascii=False)+';const CFG='+json.dumps(CFG)+';'
t=t.replace('__DATA__',data).replace('__ABOUT__',about).replace('__DATE__',datetime.date.today().strftime('%d %b %Y'))
open(os.path.join(ROOT,'index.html'),'w').write(t)
print('ok',len(t)//1024,'KB','aff',sum(1 for j in J if j['aff'] and not j['rej']))
