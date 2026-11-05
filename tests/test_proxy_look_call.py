import pytest

from llm_router.proxy.tool_classes import classify_bash, is_look_call


@pytest.mark.parametrize("command", [
    "sed -n '1,20p' f", 'grep -n "a|b" f | head -5',
    "git -C /x log --oneline -3", "cd /x && git grep foo",
    "gh pr view 12 --json state", "awk '{print $1}' f", "sort f | uniq -c",
    "cat f 2>/dev/null", "sed -n '5p;10p' f",
    "sed -n -e '/foo/p' f", "sort -r f", "uniq f", "date -u", "jq '.x' f",
    "git rev-parse HEAD", "git ls-files", "git describe", "git shortlog",
    "git merge-base HEAD main", "git for-each-ref", "git cat-file -p HEAD",
    "git cat-file -t HEAD", "git cat-file -s HEAD", "gh run list",
    "gh issue view 1", "gh repo view", "gh release list", "ollama list",
    "ollama ps", "ollama show local", "A=1 cat f", "echo 'a|b'",
] + [cmd for cmd in (
    "cut", "tr", "nl", "column", "comm", "cmp", "basename", "dirname",
    "realpath", "stat", "file", "du", "df", "which", "pwd", "ps", "pgrep",
    "uptime", "whoami", "uname", "echo", "printf", "sleep", "true",
)] )
def test_look_bash_readers(command):
    assert is_look_call("Bash", {"command": command}) is True


@pytest.mark.parametrize("command", [
    "sed -i s/a/b/ f", "sed -n '1w out' f", "sed 's/a/b/e' f",
    "awk 'BEGIN{system(\"rm x\")}'", "awk '{print > \"f\"}' g",
    "sort -o f g", "find . -delete", "git -C /x push",
    "git -C /x checkout main", "gh pr merge 1", "gh api repos/x",
    "echo hi > f", "cat $(ls)", "cat `ls`", "grep x f &", "rm -rf x",
    "curl -s u", "python3 -c 'print(1)'", "xargs rm", "cat f; rm g",
    "", '"unbalanced', "sed -n --in-place=.bak '1p' f",
    "sed -n -i.bak '1p' f", "sed -n '1r f' g", "sed -n 'e touch f' g",
    "sed -n 's/a/b/e' f", "sed -n -f script f", "awk -f script f",
    "awk '{getline}'", "awk '{print | cmd}'", "sort --output=f g",
    "sort -rofile f", "uniq input output", "date -s now", "date --set=now",
    "jq -f script f", "jq --from-file=script f", "jq --rawfile x f .",
    "jq --slurpfile x f .", "git -C /x diff --ext-diff",
    "git grep --textconv x", "A=1 git grep x", "A=1 awk '{print}'",
    "RIPGREP_CONFIG_PATH=x rg foo", "gh pr view 1 --web", "gh pr view 1 -w",
    "ollama run local", "export A=$(ls)", "export A", "echo x &&",
    "echo x ||", "echo x;", "echo <(ls)", "echo x >out", "cat f | | head",
] + [cmd for cmd in (
    "env", "printenv", "wget", "python", "node", "sqlite3", "tee", "bash",
    "sh", "eval", "for", "while", "until", "mv", "cp", "mkdir", "touch", "chmod",
)])
def test_look_bash_refusals(command):
    assert is_look_call("Bash", {"command": command}) is False


@pytest.mark.parametrize("tool_input", [None, [], "cat f", {}, {"command": None},
                                        {"command": 1}, {"command": []}])
def test_malformed_input(tool_input):
    assert is_look_call("Bash", tool_input) is False


@pytest.mark.parametrize("name", ["Read", "Grep", "Glob", "LS", "NotebookRead", "TodoRead"])
def test_technical_tools(name):
    assert is_look_call(name, {}) is True
    assert is_look_call(name, None) is False


@pytest.mark.parametrize("name", ["Edit", "Write", "Task", "WebFetch", None, [], 3])
def test_other_tools(name):
    assert is_look_call(name, {}) is False


def test_census_sed_unchanged():
    assert classify_bash("sed -n '1p' f") == "exec"


@pytest.mark.parametrize("command", ["git grep -O vim x", "git grep -Ovim x", "git grep --open-files-in-pager=vim x"])
def test_git_grep_that_runs_a_pager_is_not_a_look(command):
    assert not is_look_call("Bash", {"command": command})


@pytest.mark.parametrize("command", [
    "rg --pr\\\ne=/tmp/p x .", "rg --pr\\\r\ne=/tmp/p x .",
    "sed -n -e 1p /dev/null -e 'w /tmp/x'",
    "sed -n '1p' f --posix", "sed -n '1p' f -i", "sed -n '1p' f -f script",
    "sed -n '1d' f", "sed -n '1q' f", "sed -n '1!p' f", "sed -n -e1p f",
    "sort --out=x", "sort -o x", "sort --compress-program=x", "sort -T x",
    "sort -S 1b", "sort --files0-from=x",
    "export A=1 && rg x src", "export RIPGREP_CONFIG_PATH=/tmp/c && rg x .",
] + [f"awk {flag} script f" for flag in ("-f", "-E", "-i", "-l", "-e", "--anything")]
  + [f"{cmd} {flag}{value} x ." for cmd in ("rg", "grep")
     for flag in ("--pre", "--pre-glob", "--hostname-bin") for value in ("=x", " x")]
  + [f"file {flag}" for flag in ("-C", "--compile", "-m x", "-mx", "--magic-file x", "--magic-file=x")])
def test_review_unsafe_look_commands(command):
    assert is_look_call("Bash", {"command": command}) is False


@pytest.mark.parametrize("command", [
    "sed -n '$=' f", "sed -n -E -e '/foo/,20p' f -e '5p;10p'",
    "sort -bdfghinrsuzV f", "sort -k1,2 -t, f", "sort -k 1 -t , f",
    "sort --reverse --numeric-sort --unique f",
    "awk -F: '{print $1}' f", "awk -F : -v name=value '{print name}' f",
])
def test_review_safe_look_commands(command):
    assert is_look_call("Bash", {"command": command}) is True
