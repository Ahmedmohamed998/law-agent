"""
Seed a few conversations so the demo UI has something to show.

Writes directly through the repository — no Azure calls, so it costs nothing
and runs in a second. The answers are copied from real runs of the pipeline.

    python -m demo.seed            # seed
    python -m demo.seed --clear    # remove everything for the demo users
"""

import sys

from sqlalchemy import text

from app.db import repository as repo
from app.db.repository import SourceRow
from app.db.session import Principal, admin_session, scoped_session

# No organization, deliberately: the dev bearer token carries no `org_id`
# claim, and row-level security compares the two. Seeding under an org the
# caller doesn't claim makes the rows correctly invisible — which is the policy
# working, and a confusing way to start a demo.
USER = Principal(user_id="u_ahmed", organization_id=None)

CONVERSATIONS = [
    (
        "ar",
        [
            ("كم مدة الإجازة السنوية؟",
             "مدة الإجازة السنوية في نظام العمل السعودي **21 يوماً على الأقل** عن كل "
             "سنة خدمة، وتُزاد إلى **30 يوماً** إذا أمضى العامل **خمس سنوات متصلة** في "
             "خدمة صاحب العمل، وتُدفع بأجر مقدماً. (نظام العمل، المادة التاسعة بعد المائة)\n\n"
             "هذه معلومات عامة وليست استشارة قانونية. لحالتك الخاصة، يمكنك حجز استشارة مع المحامي.",
             "documents",
             [("labor_law_109", "نظام العمل، المادة التاسعة بعد المائة", "hybrid"),
              ("labor_law_110", "نظام العمل، المادة العاشرة بعد المائة", "hybrid")]),
            ("وهل ينطبق على العامل المنزلي؟",
             "لا ينطبق على العامل المنزلي حكم الإجازة السنوية الوارد في نظام العمل، "
             "وإنما تُنظَّم إجازته وفق **لائحة العمالة المنزلية ومن في حكمهم**: يستحق "
             "العامل المنزلي إجازة **شهراً** عند تجديد العقد بعد سنتين من الخدمة. "
             "(لائحة العمالة المنزلية ومن في حكمهم، المادة الثانية عشرة)\n\n"
             "هذه معلومات عامة وليست استشارة قانونية. لحالتك الخاصة، يمكنك حجز استشارة مع المحامي.",
             "documents",
             [("domestic_12", "لائحة العمالة المنزلية ومن في حكمهم، المادة الثانية عشرة",
               "hybrid")]),
        ],
    ),
    (
        "ar",
        [
            ("اشتغلت ست سنوات ونص والمدير قال هينهي عقدي بدون سبب. إيش حقوقي؟",
             "تترتب لك عدة حقوق:\n\n"
             "- **التعويض عن الإنهاء غير المشروع**: أجر خمسة عشر يوماً عن كل سنة خدمة "
             "للعقد غير محدد المدة. (نظام العمل، المادة السابعة والسبعون)\n"
             "- **مكافأة نهاية الخدمة**: نصف شهر عن كل سنة من السنوات الخمس الأولى، "
             "وشهر عن كل سنة تالية. (نظام العمل، المادة الرابعة والثمانون)\n\n"
             "هذه معلومات عامة وليست استشارة قانونية. لحالتك الخاصة، يمكنك حجز استشارة مع المحامي.",
             "documents",
             [("labor_law_77", "نظام العمل، المادة السابعة والسبعون", "article_lookup"),
              ("labor_law_84", "نظام العمل، المادة الرابعة والثمانون", "hybrid")]),
        ],
    ),
    (
        "ar",
        [
            ("ما هي أركان عقد البيع؟",
             "⚠️ لا تغطي المستندات المتاحة لدي أحكام عقد البيع، فهي مقتصرة على أنظمة "
             "العمل والموارد البشرية.\n\n"
             "أركان عقد البيع في النظام السعودي ثلاثة: الصيغة (الإيجاب والقبول)، "
             "والعاقدان، والمعقود عليه (المبيع والثمن). "
             "*(معرفة عامة — غير موثّقة في المستندات)*\n\n"
             "قد يكون هذا الجزء غير محدَّث؛ يُرجى التحقق منه مع المحامي.\n\n"
             "هذه معلومات عامة وليست استشارة قانونية. لحالتك الخاصة، يمكنك حجز استشارة مع المحامي.",
             "model_knowledge",
             []),
        ],
    ),
]


def clear():
    with admin_session() as db:
        n = db.execute(
            text("DELETE FROM ai.sessions WHERE user_id = :u"), {"u": USER.user_id}
        ).rowcount
    print(f"deleted {n} session(s) for {USER.user_id}")


def seed():
    for i, (lang, turns) in enumerate(CONVERSATIONS, 1):
        with scoped_session(USER) as db:
            s = repo.create_session(db, USER, lang=lang)
            for j, (question, reply, label, sources) in enumerate(turns, 1):
                repo.add_user_message(
                    db, USER, s.id, content=question,
                    client_message_id=f"seed-{i}-{j}",
                )
                repo.add_assistant_message(
                    db, USER, s.id, content=reply, source_label=label,
                    sources=[SourceRow(chunk_id=c, citation=cit, reason=r)
                             for c, cit, r in sources],
                    planned_queries={"resolved": question, "queries": [],
                                     "origin": "planned"},
                    index_version="seed", latency_ms=8400,
                )
            print(f"  {s.id}  {len(turns)} turn(s)  {turns[0][0][:40]}…")


if __name__ == "__main__":
    if "--clear" in sys.argv:
        clear()
    else:
        clear()
        print(f"seeding as {USER.user_id} (org {USER.organization_id}):")
        seed()
        print("\ndone — open the demo and send a real message to hit Azure")
