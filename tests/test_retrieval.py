"""Chunking, context headers, period ranges, rank fusion and search filters, without a
model or a database."""

import itertools
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql

from talk_is_cheap.edgar.fiscal import FiscalQuarter
from talk_is_cheap.retrieval.chunking import (
    ChunkTooLong,
    chunk_article,
    chunk_section,
    embedded_text,
)
from talk_is_cheap.retrieval.evaluate import Config, Score, answers, load_questions, normalized
from talk_is_cheap.retrieval.index import filing_header, news_header, vector_index_name
from talk_is_cheap.retrieval.search import (
    Filters,
    Mode,
    PeriodRange,
    SearchResult,
    parse_periods,
    rrf,
)
from tests.retrieval_fakes import FakeTokenizer

HEADER = "TICA · Ticacorp · FY2025 Q1 · press release · outlook"
TOKENS = FakeTokenizer()


def sentence(n: int, words: int = 9) -> str:
    """A sentence of `words` words: "Sentence 3 word word ... end."."""
    return f"Sentence {n} " + "word " * (words - 3) + "end."


def paragraph(first: int, sentences: int) -> str:
    return " ".join(sentence(first + i) for i in range(sentences))


def table(rows: int) -> str:
    lines = ["| Metric | Q1 | Q2 |", "| --- | --- | --- |"]
    lines += [f"| Revenue line {n} | ${n},000 | ${n + 1},000 |" for n in range(rows)]
    return "\n".join(lines)


def test_short_sections_are_one_chunk_with_offsets_into_the_document() -> None:
    document = "# Intro\n\nPreamble.\n\n## Outlook\n\nRevenue is expected to be $110."
    start = document.index("Revenue")
    (chunk,) = chunk_section(document[start:], start, HEADER, TOKENS)

    assert chunk.text == "Revenue is expected to be $110."
    assert document[chunk.char_start : chunk.char_end] == chunk.text
    # Header (11 words, the dots included) + text (6 words) + [CLS] and [SEP].
    assert chunk.token_count == 11 + 6 + 2


def test_chunks_end_on_paragraph_boundaries_within_the_target() -> None:
    # Ten paragraphs of 90 words: five fit in 450 tokens.
    paragraphs = [paragraph(10 * p, 10) for p in range(10)]
    text = "\n\n".join(paragraphs)
    chunks = chunk_section(text, 100, HEADER, TOKENS, overlap=0)

    assert len(chunks) == 2
    assert chunks[0].text == "\n\n".join(paragraphs[:5])
    assert chunks[1].text == "\n\n".join(paragraphs[5:])
    for chunk in chunks:
        assert text[chunk.char_start - 100 : chunk.char_end - 100] == chunk.text
        assert len(chunk.text.split()) <= 450


def test_consecutive_chunks_overlap_by_about_fifteen_percent() -> None:
    paragraphs = [paragraph(10 * p, 10) for p in range(12)]
    text = "\n\n".join(paragraphs)
    chunks = chunk_section(text, 0, HEADER, TOKENS)

    assert len(chunks) >= 3
    for before, after in itertools.pairwise(chunks):
        assert after.char_start < before.char_end  # they overlap
        shared = text[after.char_start : before.char_end]
        assert 0 < len(shared.split()) <= 0.15 * 450
        assert shared.startswith("Sentence")  # whole sentences, never part of one
        assert len(after.text.split()) <= 450


def test_a_paragraph_over_the_target_is_cut_between_sentences() -> None:
    text = paragraph(0, 80)  # 720 words in one paragraph
    chunks = chunk_section(text, 0, HEADER, TOKENS, overlap=0)

    assert len(chunks) == 2
    assert chunks[0].text.endswith("end.") and chunks[1].text.startswith("Sentence")
    assert "".join(c.text + " " for c in chunks).strip() == text


def test_a_sentence_over_the_model_limit_is_cut_between_words() -> None:
    text = "word " * 1200 + "end."
    chunks = chunk_section(text, 0, HEADER, TOKENS)

    assert len(chunks) >= 3
    for chunk in chunks:
        assert chunk.token_count <= TOKENS.max_input_tokens
        assert text[chunk.char_start : chunk.char_end] == chunk.text


def test_no_chunk_exceeds_the_model_limit_with_its_header() -> None:
    small = FakeTokenizer(max_input_tokens=128)
    text = "\n\n".join([paragraph(0, 30), table(40), "word " * 300])
    chunks = chunk_section(text, 0, HEADER, small)

    assert chunks
    assert max(c.token_count for c in chunks) <= 128
    for chunk in chunks:
        counted = len(embedded_text(HEADER, chunk.text).split()) + 2
        assert chunk.token_count == counted


def test_a_header_that_leaves_no_room_is_an_error() -> None:
    with pytest.raises(ChunkTooLong):
        chunk_section("Some text.", 0, "word " * 300, FakeTokenizer(max_input_tokens=256))


def test_an_article_too_long_for_the_model_is_an_error() -> None:
    with pytest.raises(ChunkTooLong):
        chunk_article("A headline", "word " * 100, HEADER, FakeTokenizer(max_input_tokens=64))


def test_a_table_that_fits_stays_whole_with_its_prose() -> None:
    text = f"The table below shows revenue.\n\n{table(5)}\n\nRevenue grew."
    (chunk,) = chunk_section(text, 0, HEADER, TOKENS)

    assert chunk.text == text


def test_a_long_table_is_split_by_rows_repeating_its_header() -> None:
    rows = table(120)  # about 840 words
    text = f"Revenue by line.\n\n{rows}\n\nRevenue grew."
    chunks = chunk_section(text, 50, HEADER, TOKENS)
    header = "| Metric | Q1 | Q2 |\n| --- | --- | --- |"

    pieces = [c for c in chunks if c.text.startswith(header)]
    assert len(pieces) >= 2
    covered = []
    for piece in pieces:
        span = text[piece.char_start - 50 : piece.char_end - 50]
        assert piece.text == header + "\n" + span  # header, then exactly the cited rows
        assert all(line.startswith("| Revenue line") for line in span.split("\n"))
        assert len(piece.text.split()) <= 450
        covered += span.split("\n")
    assert covered == rows.split("\n")[2:]  # every row once, in order
    assert chunks[0].text == "Revenue by line."
    assert chunks[-1].text == "Revenue grew."


def test_offsets_always_match_the_document() -> None:
    body = "\n\n".join(
        [paragraph(0, 40), table(80), "## Heading", paragraph(100, 70), "- a bullet; more."]
    )
    document = "Front matter.\n\n" + body + "\n\nBack matter."
    start = document.index(body)
    for chunk in chunk_section(body, start, HEADER, FakeTokenizer(max_input_tokens=200)):
        cited = document[chunk.char_start : chunk.char_end]
        assert chunk.text == cited or chunk.text.endswith("\n" + cited)


def test_blank_sections_have_no_chunks() -> None:
    assert chunk_section(" \n\n ", 0, HEADER, TOKENS) == []


def test_news_is_one_chunk_of_headline_and_summary() -> None:
    chunk = chunk_article("Ticacorp beats estimates", "Cloud sales jumped.", HEADER, TOKENS)
    assert chunk.text == "Ticacorp beats estimates\n\nCloud sales jumped."
    assert (chunk.char_start, chunk.char_end) == (0, len(chunk.text))
    assert chunk_article("Only a headline", None, "", TOKENS).text == "Only a headline"


def test_context_headers() -> None:
    assert (
        filing_header("NVDA", "Nvidia", 2027, 2, "press_release", "outlook")
        == "NVDA · Nvidia · FY2027 Q2 · press release · outlook"
    )
    assert (
        filing_header("MSFT", None, 2026, 4, "10k", "risk_factors")
        == "MSFT · FY2026 Q4 · 10-K · risk factors"
    )
    published = datetime(2026, 10, 2, 13, tzinfo=UTC)
    assert (
        news_header(["TSLA"], ["Tesla"], "Reuters", published)
        == "TSLA · Tesla · news · Reuters · 2026-10-02"
    )
    assert news_header([], [], None, published) == "news · 2026-10-02"
    assert embedded_text("H", "text") == "H\n\ntext"
    assert embedded_text("", "text") == "text"


def test_vector_index_names_are_valid_identifiers() -> None:
    assert (
        vector_index_name("BAAI/bge-small-en-v1.5") == "ix_embeddings_hnsw_baai_bge_small_en_v1_5"
    )
    assert len(vector_index_name("x" * 200)) == 63


def test_periods_and_ranges() -> None:
    q = FiscalQuarter
    assert parse_periods("FY2026Q3") == [PeriodRange(q(2026, 3), q(2026, 3))]
    assert parse_periods("FY2025Q1..FY2026Q4") == [PeriodRange(q(2025, 1), q(2026, 4))]
    assert parse_periods("fy2025 q1..FY2025Q2, FY2027Q2") == [
        PeriodRange(q(2025, 1), q(2025, 2)),
        PeriodRange(q(2027, 2), q(2027, 2)),
    ]
    assert parse_periods("FY2026") == [PeriodRange(q(2026, 1), q(2026, 4))]
    assert parse_periods("FY2025..FY2026Q2") == [PeriodRange(q(2025, 1), q(2026, 2))]
    assert parse_periods("FY2025Q1..FY2026Q4")[0].label == "FY2025Q1..FY2026Q4"
    for bad in ("FY2026Q4..FY2026Q1", "2026-Q3", "FY2026Q5", ""):
        with pytest.raises(ValueError):
            parse_periods(bad)


def test_reciprocal_rank_fusion() -> None:
    keyword = [1, 2, 3]
    vector = [3, 4, 1]
    fused = rrf([keyword, vector], k=60)
    scores = dict(fused)

    assert scores[1] == pytest.approx(1 / 61 + 1 / 63)
    assert scores[3] == pytest.approx(1 / 63 + 1 / 61)
    assert scores[2] == pytest.approx(1 / 62)
    assert [i for i, _ in fused] == [1, 3, 2, 4]  # ties keep first-seen order
    assert rrf([[], []]) == []
    assert [i for i, _ in rrf([[5, 6]])] == [5, 6]


def _sql(filters: Filters) -> str:
    conditions = filters.conditions()
    return " AND ".join(
        str(c.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        for c in conditions
    )


def test_filters_become_sql_conditions() -> None:
    sql = _sql(
        Filters(
            tickers=["nvda"],
            periods=parse_periods("FY2026Q1..FY2027Q2"),
            since=datetime(2026, 9, 1, tzinfo=UTC),
            sources=["filing"],
            doc_types=["press_release"],
            section_types=["outlook", "mdna"],
        )
    )
    assert "chunks.tickers && ARRAY['NVDA']" in sql
    # News counts for a ticker only through a link of at least 0.5.
    assert "article_companies.confidence >= 0.5" in sql
    assert "BETWEEN 8105 AND 8110" in sql  # FY2026Q1 and FY2027Q2 as year * 4 + quarter
    assert "chunks.published_at >= '2026-09-01 00:00:00+00:00'" in sql
    assert "chunks.source IN ('filing')" in sql
    assert "chunks.doc_type IN ('press_release')" in sql
    assert "chunks.section_type IN ('outlook', 'mdna')" in sql


def test_no_filters_only_require_a_live_source() -> None:
    (condition,) = Filters().conditions()
    sql = str(condition.compile(dialect=postgresql.dialect()))
    assert "documents.id = chunks.document_id" in sql
    assert "articles.id = chunks.article_id" in sql


def _result(**overrides: object) -> SearchResult:
    values: dict[str, object] = {
        "chunk_id": 1,
        "score": 1.0,
        "text": "Revenue is expected to be **$108.0 billion**, plus or minus 2%.",
        "header": "",
        "source": "filing",
        "tickers": ["NVDA"],
        "period": FiscalQuarter(2027, 2),
        "published_at": datetime(2026, 8, 27, tzinfo=UTC),
        "doc_type": "press_release",
        "section_type": "outlook",
        "section_title": "Outlook",
        "publisher": None,
        "url": "https://www.sec.gov/x.htm",
        "char_start": 0,
        "char_end": 10,
    }
    values.update(overrides)
    return SearchResult(**values)  # type: ignore[arg-type]


def test_benchmark_questions_and_what_answers_them(tmp_path: Path) -> None:
    lines = [
        {
            "id": "nvda-outlook",
            "question": "What revenue does Nvidia expect next quarter?",
            "filters": {"tickers": ["NVDA"]},
            "expected": {"ticker": "nvda", "period": "FY2027Q2", "section_type": "outlook"},
            "answer": ["$108.0 billion", "108 billion"],
            "kind": "paraphrase",
        },
        {
            "id": "tsla-deliveries",
            "question": "How many cars did Tesla deliver?",
            "expected": {"ticker": "TSLA", "published": ["2026-10-01", "2026-10-03"]},
            "answer": "deliveries",
        },
    ]
    path = tmp_path / "questions.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    outlook, news = load_questions(path)

    assert outlook.source == "filing" and outlook.ticker == "NVDA"
    assert outlook.filters == {"tickers": ["NVDA"]}
    assert (outlook.kind, news.kind) == ("paraphrase", "lexical")
    assert outlook.period_filters() == {"periods": "FY2027Q2"}
    assert news.period_filters() == {"since": date(2026, 10, 1), "until": date(2026, 10, 3)}
    assert answers(outlook, _result())  # markup and case are ignored
    assert not answers(outlook, _result(period=FiscalQuarter(2027, 1)))
    # The section type is recorded, not required: the CFO commentary counts too.
    assert answers(outlook, _result(section_type="commentary", doc_type="earnings_exhibit"))
    assert not answers(outlook, _result(tickers=["AMD"]))
    assert not answers(outlook, _result(text="Revenue was $46.7 billion."))

    item = _result(
        source="news",
        period=None,
        section_type=None,
        tickers=["TSLA"],
        text="Tesla Q3 deliveries beat",
        published_at=datetime(2026, 10, 3, 23, tzinfo=UTC),
    )
    assert news.source == "news" and answers(news, item)
    assert not answers(news, replace(item, published_at=datetime(2026, 10, 4, tzinfo=UTC)))
    assert not answers(news, replace(item, source="filing"))

    path.write_text(json.dumps({**lines[1], "expected": {"ticker": "TSLA"}}))
    with pytest.raises(ValueError, match=r"questions\.jsonl:1"):
        load_questions(path)
    path.write_text(json.dumps({**lines[0], "kind": "tricky"}))
    with pytest.raises(ValueError, match="kind"):
        load_questions(path)
    path.write_text("\n".join(json.dumps(lines[0]) for _ in range(2)))
    with pytest.raises(ValueError, match="repeat"):
        load_questions(path)


def test_answer_phrases_ignore_case_spacing_and_dash_types() -> None:
    assert normalized("10 - for - 1 Stock **Split**") == normalized("10-for-1 stock split")
    assert normalized("$15.8\u201316.8 billion") == normalized("$15.8-16.8  Billion")
    assert normalized("| $9.7B - $10.5B |") == "$9.7b-$10.5b"
    assert normalized("a \u2212 b \u2014 c \u2011 d") == "a-b-c-d"
    assert normalized("NVIDIA\u2019s") == "nvidia's"


def test_scores_per_group() -> None:
    score = Score(
        Config(Mode.HYBRID),
        ranks={"a": 1, "b": 4, "c": 7, "d": None},
        kinds={"a": "lexical", "b": "lexical", "c": "paraphrase", "d": "paraphrase"},
    )
    assert (score.recall(5), score.recall(10)) == (0.5, 0.75)
    assert score.mrr() == pytest.approx((1 + 1 / 4 + 1 / 7) / 4)
    assert (score.recall(5, "lexical"), score.recall(5, "paraphrase")) == (1.0, 0.0)
    assert score.mrr("paraphrase") == pytest.approx((1 / 7) / 2)
    assert Config(Mode.VECTOR, context_header=False).label == "vector (no header)"
    assert Config(Mode.RERANK, filtered=True).label == "rerank + period"
