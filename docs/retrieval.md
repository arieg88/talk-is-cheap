# Retrieval

How `talk-is-cheap index` makes filing text and news searchable, how `search()` finds
passages, and how well each search mode does on labeled questions.

## 1. How it works

1. **Chunk.** Every section of every parsed document is chunked on its own, so a chunk
   never spans two sections (`retrieval/chunking.py: chunk_section`):
   - Paragraphs (separated by blank lines) are packed into chunks of up to 450 tokens
     (`TARGET_TOKENS`), counted with the embedding model's own tokenizer.
   - A paragraph longer than that is cut between sentences, and a sentence longer than
     the model's input between words.
   - A chunk that follows prose starts with the last sentences of the chunk before, up to
     15% of 450 tokens (`OVERLAP_SHARE`). Only whole sentences are repeated.
   - A markdown table stays whole when it fits the model's input. A longer one is cut
     between rows, and every piece starts with the table's header rows (every row up to
     the `| --- |` line).
   - A news article is one chunk: headline, a blank line, and the feed's summary
     (`chunk_article`).
2. **Context header.** What is embedded is a short header, a blank line and the chunk:
   `NVDA · Nvidia · FY2027 Q2 · press release · outlook` for filings,
   `TSLA · Tesla · news · Reuters · 2026-10-02` for news (`retrieval/index.py:
   filing_header`, `news_header`). Names come from `universe.yaml`. The header is stored
   in its own column, so it can be left out (section 4).
3. **Limit.** The header, separator and text, plus the model's two special tokens, must
   fit its 512-token input (read from the tokenizer, not assumed). Chunking raises
   `ChunkTooLong` rather than let the model cut a chunk short, and the embedder checks
   every text again before embedding it.
4. **Store.** One `chunks` row per chunk, with the character range of `documents.text`
   it covers, its token count, a hash of header and text (`content_hash`) and of the text
   alone (`text_hash`). Postgres keeps an English `tsvector` of the text with a GIN index.
5. **Embed.** Vectors are stored in `embeddings`, keyed by model name and content hash,
   so a text is embedded once however often it is indexed. Each model has a partial HNSW
   index (cosine) over its own rows: vectors of different models are never searched
   together.
6. **Search.** `Searcher.search()` (`retrieval/search.py`) applies the same filters to
   every mode, then:
   - `keyword`: chunks matching any of the query's words (a question has words no
     passage contains, so all-words matching finds too little), ranked by `ts_rank`
     divided by 1 + log of the chunk's length;
   - `vector`: cosine similarity through the model's HNSW index, with
     `hnsw.iterative_scan = relaxed_order` so filters that drop rows after the index scan
     still return `k` results;
   - `hybrid`: Reciprocal Rank Fusion (k = 60) of the top 50 of each;
   - `rerank`, the default: the hybrid top 20 scored by a cross-encoder
     (Xenova/ms-marco-MiniLM-L-6-v2) with each chunk's header and text. A query and
     passage longer than its 512 tokens lose the end of the passage for scoring only.

## 2. Filters

| Filter | Applies to | Rule |
| --- | --- | --- |
| `tickers` | filings, news | A filing chunk's company. A news chunk only through a link in `article_companies` with confidence 0.5 or more, read at search time, so `ingest news --reprocess` takes effect without re-indexing. A per-company feed alone (0.3) does not count. |
| `periods` | filings | Fiscal quarters, ranges and whole years: `FY2026Q3`, `FY2025Q1..FY2026Q4`, `FY2026`, comma-separated. News has no fiscal period, so it is left out when periods are given. |
| `since`, `until` | filings, news | News publish time; a filing's filing date. A date in `until` is inclusive. |
| `sources` | both | `filing`, `news` |
| `doc_types` | both | `press_release`, `earnings_exhibit`, `10q`, `10k`, `deliveries_report`, `other_exhibit`, `news` |
| `section_types` | filings | `outlook`, `results`, `other`, `commentary`, `mdna`, `risk_factors` |

Every mode returns only chunks whose document or article still exists, so a filing
re-parsed since the last `index` is not cited with stale offsets.

## 3. Incremental indexing

`index` chunks every selected section and article again and compares each chunk with the
stored ones (`index.py: _apply`):

- a chunk identical in text, header, offsets and metadata is left alone (`unchanged`);
- a new or changed chunk is written (`added`);
- a stored chunk with no counterpart is deleted (`removed`). That covers documents
  re-parsed by `ingest filings` (new document rows) and articles deleted by
  `ingest news --reprocess`.

Only texts without a vector of the current model are embedded, so a re-parse that
leaves a section's text unchanged embeds nothing. When `EMBEDDING_MODEL` changes, the
next `index` deletes the other model's vectors and embeds everything again. At the end of
a full run, vectors no chunk uses any more are deleted.

Chunk ids are not foreign keys to documents or articles: `ingest filings` replaces a
re-parsed filing's documents, and `index` then counts the chunks it removes.

## 4. Benchmark

`talk-is-cheap eval retrieval` (`make eval-retrieval`) runs the 49 questions in
`evals/retrieval/questions.jsonl` against every configuration (`retrieval/evaluate.py`).
Each question names what a correct passage looks like, not which chunk it is, so labels
survive re-chunking: the company, the fiscal period (filings) or publish dates (news),
and a phrase or figure the passage must contain (any of several, ignoring case,
markdown, spacing and the kind of dash). The section type is recorded for each question
but not required, since the same fact appears in several documents. Search runs with
the question's filters, the company only; `--filtered` adds the question's own period or
news dates, as the agent passes them when it knows them.

- recall@k: the share of questions with a correct passage in the top k;
- MRR: the mean of 1 / rank of the first correct passage in the top 10, 0 if none.

Without the header, vectors of the chunk text alone are stored under
`<model> (text only)`, with their own HNSW index, and the reranker sees the text alone.
Keyword search reads only the chunk text, so the header does not change it.

### Questions

| Group | Questions | What it tests |
| --- | --- | --- |
| `lexical` | 35 | The question uses the passage's own key terms ("Intel revenue guidance for the third quarter of 2026") |
| `paraphrase` | 14 | The question avoids them ("How much money does Nvidia think it will bring in next quarter?", "What happens to Tesla if its CEO leaves?") |

By section: outlook 15, risk factors 11, MD&A 10, news 7, results 4, CFO commentary 2
(answered only by Nvidia's commentary, not its release or 10-Q). By company: Nvidia 7,
Intel 5, Apple 4, AMD 4, and 2 or 3 for each other company. By period: FY2024 8, FY2025
12, FY2026 16, FY2027 6. News questions use the windows stored when they were written:
Nvidia's May 2024 and Apple's July 2026 earnings, from the GDELT backfill, and October
2026 headlines. Every question has at least one correct chunk in the index.

### Results

Measured 2026-10-19 on 20,865 chunks (15 companies, 382 documents, 3,688 articles), with
BAAI/bge-small-en-v1.5 and Xenova/ms-marco-MiniLM-L-6-v2 on an 8-core laptop CPU
(i7-11800H, WSL). Recall@5 / recall@10 / MRR; milliseconds per search with the models
loaded.

Company filter only, as the questions are written:

| Configuration | All | Lexical | Paraphrase | ms |
| --- | --- | --- | --- | --- |
| keyword | 0.449 / 0.531 / 0.262 | 0.600 / 0.686 / 0.357 | 0.071 / 0.143 / 0.025 | 29 |
| vector | 0.469 / 0.510 / 0.323 | 0.543 / 0.571 / 0.390 | 0.286 / 0.357 / 0.157 | 19 |
| vector, no header | 0.449 / 0.551 / 0.314 | 0.486 / 0.600 / 0.384 | 0.357 / 0.429 / 0.140 | 19 |
| hybrid | 0.510 / 0.633 / 0.369 | 0.600 / 0.714 / 0.454 | 0.286 / 0.429 / 0.157 | 56 |
| hybrid, no header | 0.551 / 0.592 / 0.331 | 0.629 / 0.686 / 0.404 | 0.357 / 0.357 / 0.148 | 55 |
| **rerank** | **0.571 / 0.653 / 0.400** | 0.629 / 0.743 / 0.457 | 0.429 / 0.429 / 0.256 | 1,475 |
| rerank, no header | 0.531 / 0.633 / 0.355 | 0.600 / 0.686 / 0.412 | 0.357 / 0.500 / 0.214 | 1,356 |

Company and the question's period (or news dates), as the agent calls search:

| Configuration | All | Lexical | Paraphrase | ms |
| --- | --- | --- | --- | --- |
| hybrid | 0.755 / 0.857 / 0.601 | 0.829 / 0.943 / 0.661 | 0.571 / 0.643 / 0.450 | 66 |
| hybrid, no header | 0.776 / 0.857 / 0.584 | 0.857 / 0.914 / 0.635 | 0.571 / 0.714 / 0.457 | 120 |
| **rerank** | **0.857 / 0.939 / 0.649** | 0.943 / 1.000 / 0.712 | 0.643 / 0.786 / 0.491 | 1,436 |
| rerank, no header | 0.755 / 0.878 / 0.599 | 0.857 / 0.914 / 0.652 | 0.500 / 0.786 / 0.467 | 1,411 |

Reproduce with `talk-is-cheap eval retrieval --details` and
`talk-is-cheap eval retrieval --modes hybrid,rerank --filtered --details`.

### Choices made by these numbers

- **Default mode: `rerank`.** It is best on every overall measure in both settings: with
  the period filter it finds a correct passage in the top 5 for 86% of questions and in
  the top 10 for 94%, against 76% and 86% for hybrid, and the gap is widest on
  paraphrases (recall@10 0.79 against 0.64). It costs about 1.4 seconds a search on CPU
  against 60 ms for hybrid, small next to an agent's model call; pass `mode="hybrid"`
  where latency matters more.
- **Rerank depth 20.** Re-ordering the hybrid top 10, 20 or 30, with the period filter:
  0.796 / 0.837 / 0.616 at 762 ms, 0.837 / 0.918 / 0.628 at 1,438 ms, and
  0.837 / 0.918 / 0.626 at 2,140 ms (measured before the Cisco label below was widened).
  20 finds as much as 30 in two thirds of the time.
- **Context header: on.** It helps the modes that matter: rerank gains 0.06 recall@10
  with the period filter and 0.02 without, hybrid 0.04 without (equal with). Vector
  search alone does slightly better without it on paraphrases, where the header's
  company and period words add little to a question that already names them.
- **Keyword ranking: `ts_rank` over 1 + log(length).** With `ts_rank_cd` (cover density,
  unnormalized) keyword recall@10 was 0.18 and dragged hybrid (0.41) below vector alone;
  `ts_rank_cd` normalized by length scored 0.18 and `ts_rank` normalized by length 0.14.
  `ts_rank` with log-length normalization scores 0.51 and lifts hybrid to 0.61. Matching
  any word, cover density rewards long passages full of common words ("revenue",
  "quarter").

These choices were made on the same 49 questions they are reported on; there is no
held-out set, so the differences of one or two questions (2-4 points) between close
configurations are within noise. The large ones (rerank over hybrid with the period
filter, the keyword ranking, filtering by period) are not.

### What the misses show

- **Periods matter more than the mode.** Without the period filter, most misses are a
  correct passage from the wrong period: risk factors and MD&A text repeat in every
  10-Q and 10-K, so "Apple risk factors about tariffs in its latest 10-K" finds tariff
  passages from several years. The search has no notion of "latest"; the agent should
  resolve relative periods and pass `periods`. With the period filter, recall@10 rises
  from 0.65 to 0.94.
- **Paraphrases.** The three questions rerank misses with the period filter are
  paraphrases: Meta's 2026 capex outlook asked as "spend on data centers and servers this
  year" (the 10-Q's data center lease passage ranks first), Qualcomm's thin-modem risk
  asked as "less profitable to sell to its largest smartphone customer", and Oracle's
  debt risk asked as "Could Oracle's borrowing hurt the company?". Keyword search finds
  one paraphrase in 14.
- **Labels.** One label was too narrow when first run: Cisco's release states its
  guidance twice, as "$14.65 billion to $14.85 billion" and in a table as
  "$14.65 billion - $14.85 billion"; the label accepts both.

## 5. Current index

Indexed 2026-10-19 (`talk-is-cheap index`):

| Source | Section | Chunks | Largest (tokens) | Average |
| --- | --- | --- | --- | --- |
| filing | risk_factors | 6,128 | 473 | 389 |
| filing | mdna | 4,952 | 512 | 396 |
| filing | results | 3,771 | 512 | 288 |
| filing | other | 1,616 | 512 | 264 |
| filing | commentary | 434 | 512 | 339 |
| filing | outlook | 276 | 470 | 302 |
| news | | 3,688 | 187 | 41 |

- 20,865 chunks, 20,836 vectors (identical texts share one). The largest embedded input
  is 512 tokens, header and special tokens included; re-encoding every chunk with
  fastembed's own tokenizer matched the stored count and truncated none. The 14 chunks of
  exactly 512 tokens are tables kept whole.
- The first full index took 44 minutes, almost all of it embedding; running `index`
  again took 16 seconds and embedded nothing.
- The database grew by 152 MB: chunks 67 MB, embeddings 84 MB (41 MB of it the HNSW
  index). The benchmark's 16,978 text-only vectors add 69 MB more (274 MB in all).
- Search latency with the models loaded: keyword about 8 ms, vector 38 ms, hybrid 49 ms,
  rerank 1.4 to 1.6 seconds. A `talk-is-cheap search` command takes about 0.7 seconds
  more to start and load the models.

### Embedding speed

Measured on an i7-11800H (8 cores, 16 threads, `nproc` 16, AVX-512 VNNI) with 7.6 GB of
memory under WSL, embedding 512-token chunks with the quantized ONNX model fastembed
ships:

- Threads: ONNX Runtime's default (all 16) was fastest; 4 threads gave 4.9 texts/s and
  1 thread 2.4.
- Several processes (`parallel=8`) ran out of memory and were killed.
- Batch size, on 192 real chunks: 13.0 chunks/s at 1, 8.0 at 4, 5.9 at 16, 5.4 at 32.
  Chunks differ in length and a batch pads them all to its longest, so the embedder
  sends one text at a time (`encoders.py: FastEmbedEmbedder`). The vectors are the same
  as in a batch, up to float rounding. The first full index ran at batch size 16, about
  6 chunks/s; at batch size 1 it would take about 20 minutes.
- Not tried: two processes of 8 threads each, or the unquantized model.
