"""Context for a plan an executive announced: the most relevant passage of the 10-Qs and
10-Ks the company filed in the months after the statement (restructuring charges in the
next 10-Q, a buyback in the 10-K), found with the filing search the agent uses
(`search_filings`: reranked hybrid search). Shown with the plan, never as a verdict."""

from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from talk_is_cheap.db.models import Statement
from talk_is_cheap.statements.outcomes import Context, FindContext

if TYPE_CHECKING:
    from talk_is_cheap.retrieval.encoders import Embedder, Reranker

# Filings searched: those filed within this long after the statement.
CONTEXT_WINDOW = timedelta(days=200)
CONTEXT_CHARS = 600
DOC_TYPES = ("10q", "10k")
SECTION_TYPES = ("mdna", "risk_factors")


def filing_context(
    embedder: Callable[[], "Embedder"], reranker: Callable[[], "Reranker"]
) -> FindContext:
    """A `FindContext` over the filing search, loading the search models on first use."""
    from talk_is_cheap.retrieval.search import Searcher

    async def find(session: AsyncSession, row: Statement) -> Context | None:
        searcher = Searcher(session, embedder, reranker)
        results = await searcher.search(
            row.claim,
            tickers=[row.ticker],
            since=row.statement_date,
            until=row.statement_date + CONTEXT_WINDOW,
            sources=["filing"],
            doc_types=list(DOC_TYPES),
            section_types=list(SECTION_TYPES),
            k=1,
        )
        if not results:
            return None
        best = results[0]
        period = f" {best.period.label}" if best.period else ""
        form = {"10q": "10-Q", "10k": "10-K"}.get(best.doc_type, best.doc_type)
        label = f"{form}{period} · {best.section_type}"
        text = " ".join(best.text.split())
        if len(text) > CONTEXT_CHARS:
            text = text[:CONTEXT_CHARS].rsplit(" ", 1)[0] + " …"
        return Context(f"{best.url}#chars={best.char_start}-{best.char_end}", label, text)

    return find
