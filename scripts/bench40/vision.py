"""8 vision cases — the category that is 80.5% of the real token spend.

The screenshots are GENERATED with known content, so scoring is mechanical: a
number, a label, or a count that is either in the answer or is not. Real
screenshots would need a human to say what is in them, which is exactly the
judgement this benchmark must not depend on.

They imitate what the last 5 days actually contained: UI review shots — forms,
tables, status badges, nav, an error state, a chart.
"""
import base64, io, json, sys, time, urllib.request
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).parent / "shots"
OUT.mkdir(exist_ok=True)

def font(sz):
    for p in ("/System/Library/Fonts/Supplemental/Arial.ttf",
              "/System/Library/Fonts/Helvetica.ttc"):
        try: return ImageFont.truetype(p, sz)
        except OSError: continue
    return ImageFont.load_default()

BG, INK, MUTE = (250, 250, 252), (24, 26, 32), (110, 116, 128)

def canvas(w=900, h=620):
    im = Image.new("RGB", (w, h), BG)
    return im, ImageDraw.Draw(im)

def save(im, name):
    p = OUT / f"{name}.png"; im.save(p); return p

def make_form():
    im, d = canvas()
    d.text((40, 34), "Create Account", font=font(30), fill=INK)
    for i, (label, val) in enumerate([("Full name", "Dana Okoro"),
                                      ("Email", "dana@example.com"),
                                      ("Country", "Portugal"),
                                      ("Plan", "Business")]):
        y = 110 + i * 82
        d.text((40, y), label, font=font(15), fill=MUTE)
        d.rectangle([40, y + 24, 560, y + 62], outline=(205, 209, 216), width=2)
        d.text((52, y + 33), val, font=font(18), fill=INK)
    d.rectangle([40, 470, 210, 516], fill=(26, 92, 200))
    d.text((78, 483), "Continue", font=font(17), fill=(255, 255, 255))
    d.text((240, 486), "Cancel", font=font(17), fill=MUTE)
    return save(im, "form")

def make_table():
    im, d = canvas()
    d.text((40, 32), "Invoices", font=font(28), fill=INK)
    cols = ["ID", "Customer", "Amount", "Status"]
    rows = [["INV-101", "Acme Ltd", "$1,240", "Paid"],
            ["INV-102", "Globex", "$880", "Overdue"],
            ["INV-103", "Initech", "$2,310", "Paid"],
            ["INV-104", "Umbrella", "$540", "Pending"],
            ["INV-105", "Stark Co", "$4,100", "Paid"]]
    xs = [40, 190, 430, 610]
    for x, c in zip(xs, cols):
        d.text((x, 96), c, font=font(14), fill=MUTE)
    d.line([40, 120, 860, 120], fill=(220, 224, 230), width=2)
    for i, r in enumerate(rows):
        y = 140 + i * 56
        for x, cell in zip(xs, r):
            col = INK
            if cell == "Overdue": col = (184, 40, 36)
            if cell == "Pending": col = (168, 110, 10)
            d.text((x, y), cell, font=font(17), fill=col)
        d.line([40, y + 40, 860, y + 40], fill=(238, 240, 244), width=1)
    return save(im, "table")

def make_error():
    im, d = canvas()
    d.rectangle([40, 40, 860, 150], fill=(253, 236, 234), outline=(214, 90, 80), width=2)
    d.text((64, 62), "Payment failed", font=font(22), fill=(150, 30, 26))
    d.text((64, 100), "Card declined by issuer. Error code 51.",
           font=font(16), fill=(150, 30, 26))
    d.text((40, 200), "Try another payment method", font=font(18), fill=INK)
    d.rectangle([40, 250, 240, 296], fill=(26, 92, 200))
    d.text((72, 263), "Retry payment", font=font(15), fill=(255, 255, 255))
    return save(im, "error")

def make_dashboard():
    im, d = canvas()
    d.text((40, 30), "Overview", font=font(28), fill=INK)
    stats = [("Active users", "8,421"), ("Revenue", "$52,900"),
             ("Churn", "2.4%"), ("Tickets", "17")]
    for i, (k, v) in enumerate(stats):
        x = 40 + i * 210
        d.rectangle([x, 90, x + 185, 190], outline=(220, 224, 230), width=2)
        d.text((x + 16, 108), k, font=font(13), fill=MUTE)
        d.text((x + 16, 134), v, font=font(26), fill=INK)
    d.text((40, 230), "Signups per month", font=font(16), fill=MUTE)
    bars = [40, 72, 58, 95, 120, 88]
    labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun"]
    for i, (h, lab) in enumerate(zip(bars, labels)):
        x = 60 + i * 100
        d.rectangle([x, 480 - h * 2, x + 56, 480], fill=(26, 92, 200))
        d.text((x + 12, 492), lab, font=font(13), fill=MUTE)
    return save(im, "dashboard")

def make_nav():
    im, d = canvas(900, 520)
    d.rectangle([0, 0, 240, 520], fill=(28, 32, 40))
    items = ["Dashboard", "Projects", "Team", "Billing", "Settings"]
    for i, it in enumerate(items):
        y = 70 + i * 56
        if it == "Billing":
            d.rectangle([12, y - 12, 228, y + 30], fill=(48, 56, 70))
        d.text((32, y), it, font=font(17),
               fill=(255, 255, 255) if it == "Billing" else (168, 176, 190))
    d.text((280, 40), "Billing", font=font(30), fill=INK)
    d.text((280, 100), "Next invoice: 14 October 2026", font=font(17), fill=MUTE)
    return save(im, "nav")

CASES = [
    ("form-value", make_form, "What value is in the Country field? Answer with just the value.",
     lambda o: "portugal" in (o or "").lower()),
    ("form-button", make_form, "What does the primary blue button say?",
     lambda o: "continue" in (o or "").lower()),
    ("table-overdue", make_table,
     "In this invoice table, which customer's invoice is Overdue? Answer with the company name.",
     lambda o: "globex" in (o or "").lower()),
    ("table-count", make_table, "How many invoices have the status Paid? Answer with a number.",
     lambda o: "3" in (o or "")),
    ("table-amount", make_table, "What is the amount of invoice INV-105?",
     lambda o: "4,100" in (o or "") or "4100" in (o or "")),
    ("error-code", make_error, "What error code is shown? Answer with the number.",
     lambda o: "51" in (o or "")),
    ("dash-metric", make_dashboard, "What is the Revenue figure shown?",
     lambda o: "52,900" in (o or "") or "52900" in (o or "")),
    ("nav-selected", make_nav, "Which item in the left sidebar is currently selected?",
     lambda o: "billing" in (o or "").lower()),
]


def ask(model, image_path, prompt, timeout=180):
    b64 = base64.b64encode(Path(image_path).read_bytes()).decode()
    body = json.dumps({
        "model": model, "stream": False, "think": False,
        "options": {"temperature": 0.1},
        "messages": [{"role": "user", "content": prompt, "images": [b64]}],
    }).encode()
    req = urllib.request.Request("http://localhost:11434/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read()).get("message", {}).get("content", "")


if __name__ == "__main__":
    model = sys.argv[1] if len(sys.argv) > 1 else "qwen3.5:latest"
    rows = []
    for name, maker, prompt, check in CASES:
        path = maker()
        t0 = time.time()
        try:
            out = ask(model, path, prompt); err = None
        except Exception as e:
            out, err = "", f"{type(e).__name__}: {e}"
        dt = time.time() - t0
        ok = bool(check(out)) if not err else False
        rows.append({"case": name, "ok": ok, "s": round(dt, 1),
                     "out": (out or "")[:120], "err": err})
        print(f"{name:16s} {'PASS' if ok else 'FAIL'} {dt:6.1f}s  "
              f"{(out or err or '')[:70]!r}", flush=True)
    n = len(rows)
    print(f"\n{model}: {sum(r['ok'] for r in rows)}/{n} = "
          f"{sum(r['ok'] for r in rows)/n:.0%}  ({sum(r['s'] for r in rows):.0f}s)")
    json.dump(rows, open(Path(__file__).parent / f"vision_{model.replace(':','_')}.json", "w"),
              indent=2)
