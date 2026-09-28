#!/bin/zsh
# usage: runcase.sh <case_id> <tag>
S=${SPIKE_DIR:?set SPIKE_DIR to a scratch dir containing home/ and logs/}
F=~/Projects/rsi-engine-probe/holdouts-src/routing-golden-v1
id=$1; tag=$2; W=$S/runs/$tag-$id
rm -rf $W; mkdir -p $S/runs; cp -R $F/fixture_repo $W
prompt=$(python3 -c "import json,sys;[print(json.loads(l)['prompt']) for l in open('$F/constructed_cases.jsonl') if json.loads(l)['id']=='$id']")
tf=$(python3 -c "import json;[print(json.loads(l)['grader']['test_file']) for l in open('$F/constructed_cases.jsonl') if json.loads(l)['id']=='$id']")
echo "{\"case\":\"$id\",\"tag\":\"$tag\",\"start\":$(date +%s)}" >> $S/logs/cases.jsonl
cd $W && HOME=$S/home ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude -p "$prompt" --model sonnet --setting-sources project --strict-mcp-config --no-session-persistence --permission-mode acceptEdits --allowedTools "Bash(python3 tests/*)" "Bash(python tests/*)" --max-budget-usd 0.5 < /dev/null > $W.claude.out 2>&1
rc=$?
cd $W && python3 $tf > $W.pytest.out 2>&1; prc=$?
echo "{\"case\":\"$id\",\"tag\":\"$tag\",\"end\":$(date +%s),\"claude_rc\":$rc,\"test_rc\":$prc}" >> $S/logs/cases.jsonl
echo "$id $tag claude_rc=$rc test_rc=$prc :: $(tail -1 $W.claude.out | cut -c1-150)"
