"""Judge each failed forger-written test: was the model's expected value wrong, the tool wrong, or both?"""
import json, re, sys, collections
sys.path.insert(0, "tests/bench")
from bench_helpers import worked_out

PAT = re.compile(r"case (\d+): expected '(.*?)', got '(.*?)'\)?$")

def same(a, b):
    a, b = a.strip(), b.strip()
    if a == b: return True
    try: return abs(float(a) - float(b)) < 1e-9
    except ValueError: return False

def strict_same(a, b):
    return " ".join(a.split()) == " ".join(b.split())

models = ["minimax-m3", "kimi-k2.7-code", "deepseek-v4-pro", "qwen3-coder"]
grand = collections.Counter()
rows = []
for m in models:
    d = json.load(open(f"results/{m}-hard-v2.json"))
    cat = collections.Counter()
    forger_fail = collections.Counter()
    for run in d["runs"]:
        for r in run:
            steps = [s for s in r["trace"] if s["kind"] == "forge" and s["text"] != "forge budget used up"]
            drafts = list(r["drafts"])
            for st in steps:
                if st["ok"]:
                    if drafts: drafts.pop(0)
                    continue
                text = st["text"]
                if text.startswith("forger failed"):
                    forger_fail[text[:60]] += 1
                    continue
                draft = drafts.pop(0) if drafts else None
                mt = PAT.search(text)
                if "staging-test-failed" in text and mt and draft:
                    n, exp, got = int(mt.group(1)), mt.group(2), mt.group(3)
                    case = draft["tests"][n - 1] if len(draft["tests"]) >= n else None
                    if not case:
                        cat["unjudgeable: case not recorded"] += 1; continue
                    try:
                        truth = worked_out(r["task_id"], case["files"])
                    except Exception as e:
                        cat["unjudgeable: fixture does not fit the task format"] += 1; continue
                    e_ok, t_ok = same(exp, truth), same(got, truth)
                    if e_ok and not t_ok: k = "tool wrong (model's expectation was right)"
                    elif t_ok and not e_ok:
                        k = "format only (same number, different text)" if same(exp, got) else "model's expectation wrong (tool was right)"
                    elif e_ok and t_ok: k = "format only (same number, different text)"
                    else: k = "both wrong"
                    cat[k] += 1
                elif "staging-test-error" in text:
                    cat["tool crashed on a test case"] += 1
                elif "staging-exit-code" in text or "staging-timeout" in text:
                    cat["tool failed on the real inputs"] += 1
                else:
                    cat[text[:70]] += 1
    print(f"\n=== {m}")
    for k, v in cat.most_common(): print(f"  {v:3d}  {k}")
    for k, v in forger_fail.items(): print(f"  {v:3d}  FORGER: {k}")
    grand.update(cat)
print("\n=== ALL FOUR open-weight models, second batch")
for k, v in grand.most_common(): print(f"  {v:3d}  {k}")
