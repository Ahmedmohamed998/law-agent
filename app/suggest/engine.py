"""
Running the classifier beside the answer, not after it.

The answer takes ten to twenty seconds to generate; the classifier takes
one or two and needs only the question. So it is started on a worker
thread as soon as the question is known, and collected when the answer is
done — by which time it has long finished. If it has not, it is dropped:
a suggestion is never worth delaying `done` for.
"""

import logging
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError

from app.config import settings
from app.db.session import Principal
from app.suggest.catalog import catalog
from app.suggest.classify import Suggestion, classify, should_consider

log = logging.getLogger(__name__)

# Small and bounded. Each job is one short model call; under load the
# queue grows and late results are simply dropped at collection time.
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="suggest")

# How long `collect` waits past the end of the answer. Generous for a job
# that has had the whole generation to finish, tight for the client.
COLLECT_TIMEOUT_S = 1.5


def is_staff(principal: Principal) -> bool:
    return not principal.anonymous and principal.role in settings().unlimited_roles


def start(
    principal: Principal,
    content: str,
    history: list[tuple[str, str]],
    *,
    already: set[str],
    last_answer_suggested: bool,
) -> Future | None:
    """Kick off the classifier if the gates pass; else None."""
    s = settings()
    if not s.suggestions_enabled:
        return None
    offerable = catalog().offerable()
    if not should_consider(
        content,
        staff=is_staff(principal),
        already=already,
        last_answer_suggested=last_answer_suggested,
        offerable=offerable,
    ):
        return None
    # Services already proposed in this conversation are not offered again.
    remaining = tuple(x for x in offerable if x.slug not in already)
    if not remaining:
        return None
    return _pool.submit(classify, content, history, remaining)


def collect(future: Future | None) -> Suggestion | None:
    if future is None:
        return None
    try:
        return future.result(timeout=COLLECT_TIMEOUT_S)
    except TimeoutError:
        log.info("suggestion dropped: classifier still running at answer end")
        future.cancel()
        return None
    except Exception as exc:  # defensive: classify() should not raise
        log.warning("suggestion failed: %s", str(exc)[:200])
        return None
