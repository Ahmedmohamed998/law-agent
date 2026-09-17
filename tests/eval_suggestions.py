"""
Run the suggestion classifier against the real model, on the evaluation set.

    python -m tests.eval_suggestions                  # catalogue from BACKEND_URL
    python -m tests.eval_suggestions --local          # the three built-in services

Not part of the test suite: it spends money and needs MANTLE_BEARER_TOKEN.
Run it after editing a service's hint in the dashboard, or after lowering
SUGGEST_MIN_CONFIDENCE, to see what the change does before clients do.

Prints one line per message and a confusion summary. A wrong suggestion
(a service where none was expected) is the failure that matters most: a
missed one costs a sale, a wrong one costs trust.
"""

import json
import sys
from pathlib import Path

from app.suggest import classify
from app.suggest.catalog import Catalog, catalog

LOCAL = [
    {"slug": "consultation", "name": "استشارة قانونية", "price_cents": 50000,
     "ai_hint": "عندما تحتاج حالة العميل إلى رأي محامٍ مباشر أو متابعة شخصية لا يكفي فيها الجواب العام."},
    {"slug": "contract-review", "name": "مراجعة عقد", "price_cents": 30000,
     "ai_hint": "عندما يريد العميل فهم أو تعديل أو مراجعة عقد عمل أو ملحق قبل توقيعه."},
    {"slug": "dispute", "name": "إنهاء نزاع مع صاحب العمل", "price_cents": 80000,
     "ai_hint": "عندما يوجد خلاف قائم مع صاحب العمل حول مستحقات أو فصل أو راتب متأخر ويريد العميل التصعيد أو الشكوى."},
]


def main(argv: list[str]) -> int:
    if "--local" in argv:
        cat = Catalog(base_url="https://unused.example", ttl=10**9)
        cat.load(LOCAL)
    else:
        cat = catalog()
    offerable = cat.offerable()
    if not offerable:
        print("no offerable services (set BACKEND_URL, or pass --local)")
        return 2

    rows = [
        json.loads(line)
        for line in Path(__file__).with_name("suggestions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    right = wrong = missed = 0
    for r in rows:
        verdict = classify.classify(r["message"], [], offerable)
        got = verdict.service.slug if verdict else None
        expect = r["expect"]
        if got == expect:
            right += 1
            mark = "ok  "
        elif got is None:
            missed += 1
            mark = "MISS"
        else:
            wrong += 1
            mark = "WRONG" if expect is None else "swap"
        conf = f"{verdict.confidence:.2f}" if verdict else "    "
        print(f"{mark:5} {conf} expect={expect!s:16} got={got!s:16} {r['message'][:60]}")

    total = len(rows)
    print(f"\n{right}/{total} right · {missed} missed · {wrong} wrong (service where none, or the wrong one)")
    return 0 if wrong == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
