#!/usr/bin/env python3
"""ENGINE_POLICY complexity on the GT-500 TUNE split only (PLAN v16 P1.6, D-69, D-19).

Scores engine.decide's tier against the Opus rubric label on the 108 tune items: exact, under-route,
raw cost and C-lambda2 (COST haiku 1.0 / sonnet 2.7 / opus 6.15, under-route costs 2x the true tier;
eval_router.py), with Wilson CIs and a session-clustered bootstrap of C-lambda2 against rules_eff
(GATEWAY_POLICY on the last 3,000 chars) and hook_eff (HOOK_LIVE_POLICY). "eff" lifts haiku to sonnet
(haiku_rewrite is false). The held-out split is never read: held-out rows are skipped before use.
Prints aggregates only (no prompt text).   GT500_DIR=... python scripts/engine_policy_tune.py
"""
from __future__ import annotations

import json, os
G=os.environ.get("GT500_DIR", "/Users/yaliandrona/.rsi/research/primary-plan/v16/gt500/")
sp=json.load(open(G+"split.json")); tune=set(sp["tune_ids"]); held=set(sp["heldout_ids"])
assert not tune&held
items={}
for l in open(G+"items.jsonl"):
    r=json.loads(l)
    if r["id"] in tune: items[r["id"]]=r          # held-out rows are skipped before use
def labels(f):
    d={}
    for l in open(G+f):
        r=json.loads(l)
        if r["id"] in tune: d[r["id"]]=r["tier"]
    return d
opus=labels("work/labels_opus.jsonl"); sonnet=labels("work/labels_sonnet.jsonl")
ids=sorted(i for i in items if i in opus)
TUNE=[{"id":i,"text":(items[i].get("context_withheld") and "" or "")+items[i]["prompt"],"truth":opus[i],"truth_sonnet":sonnet.get(i),"session":items[i]["session_id"]} for i in ids]

import math, random, collections
RANK={"haiku":1,"sonnet":2,"opus":3}; COST={"haiku":1.0,"sonnet":2.7,"opus":6.15}
MAP={"simple":"haiku","moderate":"sonnet","complex":"opus","deep_reasoning":"opus"}
def lift(t): return "sonnet" if t=="haiku" else t
def wilson(k,n,z=1.959964):
    p=k/n; d=1+z*z/n; c=(p+z*z/(2*n))/d; h=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/d; return [round(max(0,c-h),3),round(min(1,c+h),3)]
def cost(p,t,lam=2.0): return COST[p]+(lam*COST[t] if RANK[p]<RANK[t] else 0.0)
def metrics(preds, key="truth"):
    n=len(TUNE); ex=sum(preds[i["id"]]==i[key] for i in TUNE); ur=sum(RANK[preds[i["id"]]]<RANK[i[key]] for i in TUNE)
    craw=sum(COST[preds[i["id"]]] for i in TUNE)/n; cl2=sum(cost(preds[i["id"]],i[key]) for i in TUNE)/n
    return {"n":n,"exact":ex,"exact_ci":wilson(ex,n),"under":ur,"under_ci":wilson(ur,n),"over":n-ex-ur,"craw":round(craw,3),"cl2":round(cl2,3)}
def boot(predsA,predsB,B=2000,seed=20261008):
    by=collections.defaultdict(list)
    for i in TUNE: by[i["session"]].append(i)
    ks=sorted(by); rng=random.Random(seed)
    f=lambda i:cost(predsA[i["id"]],i["truth"])-cost(predsB[i["id"]],i["truth"])
    def d(s):
        tot=[i for k in s for i in by[k]]; return sum(f(i) for i in tot)/len(tot)
    bs=sorted(d([rng.choice(ks) for _ in ks]) for _ in range(B))
    return {"diff":round(d(ks),3),"ci95":[round(bs[int(.025*B)],3),round(bs[int(.975*B)-1],3)],"clusters":len(ks)}
def eff(preds): return {k:lift(v) for k,v in preds.items()}


def main() -> int:
    from llm_router import classify as C, engine

    engine._cache_mode = lambda: "off"
    eng = {i["id"]: engine.decide(i["text"][-3000:], door="gateway").tier for i in TUNE}
    rules = {i["id"]: MAP[C.classify_signals(i["text"][-3000:], C.GATEWAY_POLICY).complexity.value] for i in TUNE}
    hook = {i["id"]: MAP[C.classify_signals(i["text"], C.HOOK_LIVE_POLICY).complexity.value] for i in TUNE}
    for nm, p in (("engine_eff", eff(eng)), ("rules_eff", eff(rules)), ("hook_eff", eff(hook)),
                  ("engine_raw", eng), ("rules_raw", rules)):
        print(nm, metrics(p))
    print("engine_eff vs sonnet labels", metrics(eff(eng), "truth_sonnet"))
    print("boot C-lambda2 engine - rules_eff", boot(eff(eng), eff(rules)))
    print("boot C-lambda2 engine - hook_eff", boot(eff(eng), eff(hook)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
