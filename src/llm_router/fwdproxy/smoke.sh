#!/bin/bash
# D-607 deploy smoke: one -p call through claude-rc; prints counts only.
set -u
T0=$(date +%s); LOG=~/.llm-router/logs/fwdproxy.log; N0=$(wc -l < "$LOG" 2>/dev/null || echo 0)
cd /tmp && perl -e 'alarm 150; exec @ARGV' ~/.llm-router/bin/claude-rc -p "Count from 1 to 20, one number per line" --max-turns 1 </dev/null >/tmp/fwd-smoke.out 2>/tmp/fwd-smoke.err
echo "smoke rc=$? secs=$(( $(date +%s)-T0 )) lines=$(wc -l < /tmp/fwd-smoke.out) last=$(tail -1 /tmp/fwd-smoke.out)"
grep -c 'unrouted' /tmp/fwd-smoke.err | sed 's/^/unrouted-notice lines: /'
echo "fwdproxy.log new lines by mode/host:"; tail -n +$((N0+1)) "$LOG" | awk '{print $2,$3}' | sort | uniq -c
/usr/bin/python3 - "$T0" <<'EOF'
import json,sys,os,collections
t0=float(sys.argv[1]); c=collections.Counter()
for l in open(os.path.expanduser('~/.llm-router/proxy_calls.jsonl')):
    try: d=json.loads(l)
    except Exception: continue
    t=d.get('ts')
    if isinstance(t,(int,float)) and t>=t0 and str(d.get('session_id',''))[:8]!='b138aa25':
        c[(d.get('is_main_thread'),d.get('served_by'),d.get('reason'))]+=1
print('ledger rows (non-coordinator) since start:',sum(c.values()),dict(c))
EOF
