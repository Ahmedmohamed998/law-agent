"""
Scenario retrieval benchmark.

Users describe a situation rather than asking a legal question, and a scenario
usually raises several issues at once. This set fixes the expected answer for
each issue so a retrieval change can be measured instead of judged by feel.

Every `must_find` id was checked against data/chunks/chunks.jsonl and is the
provision that actually governs the issue — not merely a document that mentions
it. Where a guide restates an article, the article is the gold answer.

    python -m eval.scenarios              # measure current settings
    python -m eval.scenarios --runs 3     # repeat, to expose instability
    python -m eval.scenarios --no-planner # force the planner off
"""

import argparse
import json
import statistics
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCENARIOS = [
    {
        "name": "إجازة الوضع",
        "message": "أنا موظفة حامل في الشهر الثامن وأشتغل في مركز تجاري، وصاحب العمل "
                   "يقول لي إن ما فيه إجازة مدفوعة وإني لازم أستقيل إذا أبي أرتاح "
                   "بعد الولادة. هل كلامه صحيح وإيش النظام يقول؟",
        "must_find": {"labor_law_151": "إجازة الوضع 12 أسبوعاً"},
    },
    {
        "name": "فصل بعد 6 سنوات + إجازات",
        "message": "السلام عليكم، أنا اشتغلت في شركة مقاولات تقريباً ست سنوات ونص، "
                   "وكنت ملتزم بالدوام وما عندي أي إنذارات. الأسبوع اللي فات المدير "
                   "قالي إنهم هينهون عقدي الشهر الجاي بدون ما يذكر أي سبب، وعندي "
                   "كمان إجازات سنوية ما أخذتهاش من سنتين. إيش حقوقي؟",
        "must_find": {
            "labor_law_77": "التعويض عن الإنهاء غير المشروع",
            "labor_law_84": "مكافأة نهاية الخدمة",
            "labor_law_111": "أجر الإجازات غير المستعملة",
        },
    },
    {
        "name": "نقل + بدل سكن + تهديد بالفصل",
        "message": "أنا موظف من ٨ سنين، ومؤخراً نقلوني لفرع ثاني في مدينة بعيدة من "
                   "غير ما يستأذنوني، وخفّضوا بدل السكن، ولما اعترضت هددوني بالفصل. "
                   "وعندي رصيد إجازات ما أخذته. أعمل إيه؟",
        "must_find": {
            "labor_law_58": "النقل يتطلب موافقة كتابية",
            "labor_law_61": "توفير السكن أو بدل نقدي",
            "labor_law_80": "حالات الفسخ دون مكافأة",
        },
    },
    {
        "name": "ساعات عمل وأجر إضافي",
        "message": "أنا أشتغل في مطعم ودوامي يبدأ من ١٢ الظهر لين ١٢ بالليل تقريباً "
                   "كل يوم بدون راحة كافية، وما يعطوني أي زيادة على الراتب مقابل "
                   "الساعات الزايدة. هل هذا نظامي؟",
        "must_find": {
            "labor_law_98": "8 ساعات يومياً / 48 أسبوعياً",
            "labor_law_107": "أجر إضافي = الساعة + 50%",
        },
    },
    {
        "name": "توطين الأجهزة الطبية",
        "message": "عندي مؤسسة صغيرة فيها أربع موظفين سعوديين وثلاثة وافدين، ونشاطنا "
                   "بيع مستلزمات طبية، وسمعت إن فيه قرار توطين جديد لازم ألتزم فيه "
                   "وإلا فيه غرامات. وش المطلوب مني بالضبط؟",
        "must_find": {"2f00d8a9_s5": "النسب المفروضة لمهن المبيعات"},
    },
    {
        # Colloquial and short: 11 tokens, and "اوفر تايم" appears nowhere in
        # the corpus, which says "العمل الإضافي".
        "name": "أوفر تايم (عامية)",
        "message": "لو بشتغل اكتر من 10 ساعات ومش باخد اوفر تايم دا قانوني ؟",
        "must_find": {
            "labor_law_98": "8 ساعات يومياً",
            "labor_law_107": "أجر إضافي = الساعة + 50%",
        },
    },
    {
        # 4 tokens after stopword removal — sat just under the planner
        # threshold, so it got no query understanding and missed the table
        # that the near-identical 6-token phrasing finds immediately.
        "name": "خصم التأخير (عامية قصيرة)",
        "message": "التاخير في العمل المفروض يتخصملي ايه ؟",
        "must_find": {"annex1_None_2": "جدول مخالفات مواعيد العمل"},
    },
    {
        "name": "استقالة ومكافأة",
        "message": "أنا شغال في شركة من ٣ سنين وحابب أستقيل عشان لقيت فرصة أحسن، بس "
                   "زميلي قالي إني لو استقلت ما راح آخذ مكافأة نهاية الخدمة كاملة. "
                   "صح كلامه ولا لأ؟",
        "must_find": {"labor_law_85": "ثلث المكافأة بعد سنتين"},
    },
]

# Direct questions must not regress while scenarios improve.
CONTROLS = [
    ("المادة 74", "labor_law_74"),
    ("ما هي مكافأة نهاية الخدمة؟", "labor_law_84"),
    ("أعطني جدول المخالفات والجزاءات المتعلقة بمواعيد العمل", "annex1_None_2"),
    ("ما هي نسبة توطين مهن المشتريات؟", "31cd468d_s5"),
]


def run(retriever, runs: int, verbose: bool = True):
    chunks = {c["chunk_id"] for c in
              (json.loads(l) for l in open(ROOT / "data/chunks/chunks.jsonl", encoding="utf-8"))}
    for s in SCENARIOS:
        missing = [g for g in s["must_find"] if g not in chunks]
        if missing:
            raise SystemExit(f"gold id not in corpus: {missing} ({s['name']})")

    totals, per_run_scores = [], []
    for run_i in range(runs):
        hit = want = 0
        if verbose:
            print(f"\n{'=' * 74}\nRUN {run_i + 1}")
        for s in SCENARIOS:
            t0 = time.time()
            got = {c.chunk_id for c in retriever.retrieve(s["message"])}
            dt = time.time() - t0
            found = [g for g in s["must_find"] if g in got]
            hit += len(found)
            want += len(s["must_find"])
            if verbose:
                miss = [f"{g} ({d})" for g, d in s["must_find"].items() if g not in got]
                print(f"  {len(found)}/{len(s['must_find'])}  {s['name']:<32} {dt:4.1f}s")
                for m in miss:
                    print(f"        missing: {m}")
        per_run_scores.append(hit / want * 100)
        totals.append((hit, want))
        if verbose:
            print(f"  --> {hit}/{want} = {hit / want * 100:.0f}%")

    ctrl_ok = 0
    for q, want_id in CONTROLS:
        got = {c.chunk_id for c in retriever.retrieve(q)}
        ok = want_id in got
        ctrl_ok += ok
        if verbose:
            print(f"  control {'OK  ' if ok else 'FAIL'} {q[:46]}")

    print(f"\n{'=' * 74}")
    print(f"scenario recall : {statistics.mean(per_run_scores):.0f}%"
          + (f"  (runs: {[f'{x:.0f}%' for x in per_run_scores]})" if runs > 1 else ""))
    print(f"controls passed : {ctrl_ok}/{len(CONTROLS)}")
    return statistics.mean(per_run_scores), ctrl_ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--no-planner", action="store_true")
    args = ap.parse_args()

    from app.rag.retriever import Retriever

    kwargs = {"expand_queries": False}
    try:
        r = Retriever(plan_scenarios=not args.no_planner, **kwargs)
    except TypeError:  # planner not installed in this build
        r = Retriever(**kwargs)
    run(r, args.runs)
