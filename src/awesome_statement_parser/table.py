"""Stage 3 — apply a LayoutSpec to a Document, deterministically.

1. Find every occurrence of the table header (1–2 line blocks; repeated per page).
2. Anchor each column on its header words, then snap column boundaries to the
   whitespace gutters observed in the data rows (handles right-aligned amounts under
   left-aligned headers, and narrations that run long).
3. Assign every word on every table line to a column; segment lines into transactions
   (a line whose date column starts with a date) + continuation lines (wrapped
   narrations, amounts on a second line), handling top- and middle-aligned rows.
4. Turn column text into typed values; resolve debit/credit direction.

Every line inside the table region gets a disposition. A line carrying money in a
debit/credit/amount column that cannot be attached to a transaction is recorded as
UNACCOUNTED — the validation layer refuses to call such a parse verified.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from statistics import median
from typing import Optional

from .models import Anchor, Document, LayoutSpec, Line, SourceRef, Transaction, UnaccountedLine, Word
from .values import (
    ParsedAmount,
    date_prefix,
    find_dates,
    infer_date_format,
    looks_like_money,
    parse_amount,
    parse_date,
    signed_balance,
)

AMOUNT_ROLES = ("debit", "credit", "amount")
MONEY_ROLES = AMOUNT_ROLES + ("balance",)

DEFAULT_SKIP_MARKERS = [
    "opening balance", "balance b/f", "balance bf", "b/f", "brought forward", "balance brought forward",
    "carried forward", "balance c/f", "c/f", "balance forward", "closing balance", "total", "grand total",
    "page total", "sub total", "subtotal", "transaction total",
]
_OPENING_MARKERS = ("opening balance", "b/f", "bf", "brought forward", "balance forward")
_CLOSING_MARKERS = ("closing balance", "c/f", "carried forward")


# Header vocabulary normalisation, so "Withdrawal Amt." matches "Withdrawals Amount".
_ABBREV = {
    "amt": "amount", "amnt": "amount", "txn": "transaction", "tran": "transaction", "trans": "transaction",
    "dt": "date", "bal": "balance", "chq": "cheque", "cheq": "cheque", "check": "cheque", "number": "no",
    "num": "no", "ref": "reference", "desc": "description", "wdl": "withdrawal", "dep": "deposit",
    "cr": "credit", "dr": "debit", "particular": "particulars", "rs": "inr",
}


def _norm_token(t: str) -> str:
    t = _ABBREV.get(t, t)
    if len(t) > 4 and t.endswith("s") and not t.endswith("ss") and t != "particulars":
        t = t[:-1]
    return _ABBREV.get(t, t)


def tokens(text: str) -> set[str]:
    toks = {_norm_token(t) for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t}
    if not toks and (text or "").strip():
        return {text.strip()}  # symbol-only headers such as "#"
    return toks


def _overlap(have: set[str], want: set[str]) -> set[str]:
    """Tokens of `want` matched by `have`: equal, or one a prefix (>= 4 chars) of the other."""
    out = set()
    for w in want:
        for h in have:
            if h == w or (min(len(h), len(w)) >= 4 and (h.startswith(w) or w.startswith(h))):
                out.add(w)
                break
    return out


# ---------------------------------------------------------------------------
# Header location
# ---------------------------------------------------------------------------


@dataclass
class HeaderHit:
    page: int
    line_ids: list[str]
    last_index: int  # index (within page) of the last header line
    spans: list[Optional[tuple[float, float]]]  # per spec column


def _line_height(ln: Line) -> float:
    return max(ln.y1 - ln.y0, 1.0)


def _has_money(ln: Line) -> bool:
    return any(looks_like_money(w.text) for w in ln.words) or looks_like_money(ln.text)


def locate_columns(words: list[Word], headers: list[str]) -> list[Optional[tuple[float, float]]]:
    """Map each header string to an x-span on the header words, left to right."""
    spans: list[Optional[tuple[float, float]]] = []
    used: set[int] = set()
    cursor = float("-inf")
    h = median([w.y1 - w.y0 for w in words]) if words else 8.0
    for header in headers:
        want = tokens(header)
        cands = [i for i, w in enumerate(words) if i not in used and _overlap(tokens(w.text), want)]
        right = [i for i in cands if words[i].x0 >= cursor - 2]
        if not want or not right:
            spans.append(None)
            continue
        first = min(right, key=lambda i: (words[i].x0, words[i].y0))
        chosen = [first]
        remaining = want - _overlap(tokens(words[first].text), want)
        x0, x1 = words[first].x0, words[first].x1
        for i in sorted(right, key=lambda i: abs(words[i].xc - words[first].xc)):
            if i == first or not remaining or not _overlap(tokens(words[i].text), remaining):
                continue
            w = words[i]
            if w.x0 <= x1 + 1.5 * h and w.x1 >= x0 - 1.5 * h:
                chosen.append(i)
                remaining -= _overlap(tokens(w.text), remaining)
                x0, x1 = min(x0, w.x0), max(x1, w.x1)
        used.update(chosen)
        cursor = words[first].x0 + 1
        spans.append((x0, x1))
    return spans


# Words a printed header for each role typically contains (after token normalisation).
_ROLE_WORDS = {
    "date": {"date", "transaction", "posting", "txn"},
    "value_date": {"value", "date", "posting"},
    "description": {"description", "narration", "particulars", "details", "remarks", "transaction", "merchant", "detail"},
    "reference": {"reference", "cheque", "no", "ref", "instrument", "utr"},
    "debit": {"debit", "withdrawal", "paid", "out", "payment", "purchase"},
    "credit": {"credit", "deposit", "paid", "in", "receipt"},
    "amount": {"amount", "inr", "value"},
    "dr_cr": {"debit", "credit", "type", "dr", "cr"},
    "balance": {"balance", "closing", "running", "available"},
    "other": set(),
}


def header_cells(block: list[Line]) -> list[list[Word]]:
    """Visual header cells of a 1–3 line header block (cells overlapping across lines merge)."""
    cells = sorted((list(c) for ln in block for c in ln.cells()), key=lambda c: c[0].x0)
    merged: list[list[Word]] = []
    for c in cells:
        if merged:
            lo, hi = min(w.x0 for w in merged[-1]), max(w.x1 for w in merged[-1])
            if c[0].x0 < hi - 1 and c[-1].x1 > lo + 1:
                merged[-1].extend(c)
                continue
        merged.append(c)
    return merged


def _similarity(cell_text: str, header: str, role: str) -> float:
    have = tokens(cell_text)
    want = tokens(header)
    text_sim = len(_overlap(have, want)) / len(want) if want else 0.0
    rw = _ROLE_WORDS.get(role, set())
    role_sim = 0.6 if rw and _overlap(have, rw) else 0.0
    return max(text_sim, role_sim)


def align_spans(block: list[Line], headers: list[str], roles: list[str]) -> list[Optional[tuple[float, float]]]:
    """Map spec columns (left to right) onto the printed header cells.

    Order-preserving alignment (DP) scored by header-text similarity and role vocabulary,
    with a small bonus for pairing at all — so a paraphrased header still lands on the
    cell in its position. A cell holding two columns' headers (tight spacing) is split
    word by word; printed cells no spec column claims are returned as extra columns
    (appended after the spec's own), so their contents cannot leak into a neighbour.
    """
    cells = header_cells(block)
    n, k = len(cells), len(headers)
    if not cells or not headers:
        return [None] * k
    texts = [" ".join(w.text for w in c) for c in cells]
    sim = [[_similarity(texts[j], headers[i], roles[i]) for j in range(n)] for i in range(k)]
    BONUS = 0.05
    # dp[i][j]: best score aligning headers[:i] with cells[:j]
    NEG = float("-inf")
    dp = [[NEG] * (n + 1) for _ in range(k + 1)]
    back = [[None] * (n + 1) for _ in range(k + 1)]
    dp[0][0] = 0.0
    for i in range(k + 1):
        for j in range(n + 1):
            if dp[i][j] == NEG:
                continue
            if i < k and j < n and dp[i][j] + sim[i][j] + BONUS > dp[i + 1][j + 1]:
                dp[i + 1][j + 1], back[i + 1][j + 1] = dp[i][j] + sim[i][j] + BONUS, "pair"
            if i < k and dp[i][j] > dp[i + 1][j]:
                dp[i + 1][j], back[i + 1][j] = dp[i][j], "skip_header"
            if j < n and dp[i][j] > dp[i][j + 1]:
                dp[i][j + 1], back[i][j + 1] = dp[i][j], "skip_cell"
    pairs: dict[int, int] = {}
    i, j = k, n
    while i or j:
        step = back[i][j]
        if step == "pair":
            pairs[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif step == "skip_header":
            i -= 1
        else:
            j -= 1

    def span(ws: list[Word]) -> tuple[float, float]:
        return (min(w.x0 for w in ws), max(w.x1 for w in ws))

    spans: list[Optional[tuple[float, float]]] = [None] * k
    for hi, cj in pairs.items():
        spans[hi] = span(cells[cj])
    # unpaired headers whose words sit inside a paired cell: split that cell
    for cj in range(n):
        owners = sorted([hi for hi, c in pairs.items() if c == cj] + [hi for hi in range(k) if hi not in pairs and sim[hi][cj] >= 0.5])
        if len(owners) > 1:
            sub = locate_columns(sorted(cells[cj], key=lambda w: w.x0), [headers[h] for h in owners])
            for h, sp in zip(owners, sub):
                spans[h] = sp
    used = set(pairs.values())
    extras = [span(cells[cj]) for cj in range(n) if cj not in used and not any(
        s and s[0] <= span(cells[cj])[0] and span(cells[cj])[1] <= s[1] for s in spans)]
    return spans + extras


def _absorb(lines: list[Line], i: int, block: list[Line], accept) -> list[Line]:
    """Extend a header block with following close lines that `accept` (two/three-line headers)."""
    while i + len(block) < len(lines):
        nxt = lines[i + len(block)]
        close = nxt.y0 - block[-1].y1 <= 0.8 * _line_height(block[-1])
        if close and nxt.words and not _has_money(nxt) and not date_prefix(nxt.text) and accept(nxt):
            block.append(nxt)
        else:
            break
    return block


def _headerish_line(ln: Line, want: set[str]) -> bool:
    """Mostly header vocabulary and no dates — not a summary line that mentions 'Date'."""
    if find_dates(ln.text):
        return False
    own = tokens(ln.text)
    return bool(own) and len(_overlap(own, want)) >= 0.5 * len(own) - 1e-9 if len(own) > 1 else bool(_overlap(own, want))


def _usable(spec: LayoutSpec, spans) -> bool:
    found = {r for r, s in zip(spec.roles(), spans[: len(spec.columns)]) if s}
    return "date" in found and bool(found & set(MONEY_ROLES))


def _scan(doc: Document, spec: LayoutSpec, want: set[str], spans_for, threshold: float) -> list[HeaderHit]:
    hits: list[HeaderHit] = []
    for page in doc.pages:
        lines = page.lines
        i = 0
        while i < len(lines):
            ln = lines[i]
            if _has_money(ln) or date_prefix(ln.text):
                i += 1
                continue
            best = None
            for size in (1, 2, 3):
                block = lines[i : i + size]
                if len(block) < size:
                    break
                if size > 1:
                    prev, nxt = block[-2], block[-1]
                    if nxt.y0 - prev.y1 > 0.8 * _line_height(prev) or _has_money(nxt) or date_prefix(nxt.text):
                        break
                got = set().union(*(tokens(b.text) for b in block))
                score = len(_overlap(got, want)) / len(want)
                if best is None or score > best[0] + 1e-9:
                    best = (score, block)
            if best and best[0] >= threshold:
                block = list(best[1])
                # a block must not start with a non-header line (e.g. "Payment Due Date : 28 Dec 25")
                if not _headerish_line(block[0], want):
                    i += 1
                    continue
                block = [b for b in block if _headerish_line(b, want)]
                # absorb a trailing line made only of header words ("Date | No.", "(Dr) | (Cr)")
                block = _absorb(lines, i, block, lambda n: _overlap(tokens(n.text), want) == tokens(n.text))
                spans = spans_for(block)
                if spans and _usable(spec, spans):
                    hits.append(HeaderHit(page.number, [b.id for b in block], block[-1].index, spans))
                    i += len(block)
                    continue
            i += 1
    return hits


def find_date(text: str, fmt: Optional[str] = None) -> Optional[str]:
    """A date at the start of a cell, or after up to two leading tokens (serial no., '*')."""
    words = (text or "").split()
    for skip in range(0, min(3, len(words))):
        p = date_prefix(" ".join(words[skip:]))
        if p and (fmt is None or parse_date(p, fmt)):
            return p
        if skip == 0 and fmt is None and p:
            return p
    return None


def _row_like(ln: Line) -> bool:
    return bool(find_date(ln.text)) and looks_like_money(ln.text)


def find_table_start(doc: Document, max_gap: int = 8) -> Optional[Line]:
    """First line of the transaction table: the start of the LONGEST run of dated amount rows.

    Not simply the first dated amount line — account-information preambles carry a few
    ("Balance as on 01 Apr 2025 : 3,326.74", a 2-row standing-instruction table), but the
    transaction table is by far the longest run. A run tolerates up to `max_gap` other
    lines between rows (wrapped narrations, page footers and repeated page headers).
    """
    best_start, best_n = None, 0
    start, n, gap = None, 0, 0
    for ln in doc.all_lines():
        if _row_like(ln):
            if start is None:
                start, n = ln, 0
            n += 1
            gap = 0
            if n > best_n:
                best_start, best_n = start, n
        elif start is not None:
            gap += 1
            if gap > max_gap:
                start, n, gap = None, 0, 0
    return best_start


_DASHES = {"-", "--", "—", "–", "NA", "N/A", "nil", "Nil"}


def _table_rows(doc: Document, start: Line, max_gap: int = 8, cap: int = 400) -> list[Line]:
    """The dated amount rows of the run that begins at `start` (see find_table_start)."""
    lines = doc.all_lines()
    pos = next(i for i, ln in enumerate(lines) if ln.id == start.id)
    rows, gap = [], 0
    for ln in lines[pos:]:
        if _row_like(ln):
            rows.append(ln)
            gap = 0
            if len(rows) >= cap:
                break
        else:
            gap += 1
            if gap > max_gap:
                break
    return rows


def columns_from_data(doc: Document) -> list[tuple[tuple[float, float], str]]:
    """Columns of a table whose header is not readable text (an image, a coloured banner),
    derived from the rows alone: whitespace gutters give the columns, cell contents give
    the roles. Returns [(x-span, role)] left to right; [] when there is no clear table.

      dates            -> date, then value_date
      amounts or "-"   -> the rightmost column filled on (nearly) every row is the balance;
                          two complementary columns (each row has one or the other) are
                          debit then credit — the balance chain later confirms the order,
                          and the pipeline also tries it swapped
      words            -> the wordiest is the description; others reference/other
    """
    start = find_table_start(doc)
    if start is None:
        return []
    rows = _table_rows(doc, start)
    if len(rows) < 3:
        return []
    occupancy: dict[int, int] = {}
    for ln in rows:
        for w in ln.words:
            for x in range(int(w.x0), int(w.x1) + 1):
                occupancy[x] = occupancy.get(x, 0) + 1
    limit = max(1, int(0.03 * len(rows)))
    gaps = sorted(b.x0 - a.x1 for ln in rows for a, b in zip(ln.words, ln.words[1:]) if 0 < b.x0 - a.x1 < 0.6 * max(ln.y1 - ln.y0, 1.0))
    min_run = max(3, int(round(1.5 * (gaps[len(gaps) // 2] if gaps else 2.0))))

    xs = sorted(x for x, n in occupancy.items() if n >= limit)
    if not xs:
        return []
    intervals: list[list[int]] = [[xs[0], xs[0]]]
    for x in xs[1:]:
        if x - intervals[-1][1] - 1 >= min_run:
            intervals.append([x, x])
        else:
            intervals[-1][1] = x

    def cells(iv) -> list[str]:
        return [" ".join(w.text for w in ln.words if iv[0] <= w.xc <= iv[1] + 1) for ln in rows]

    kinds = []
    for iv in intervals:
        cs = cells(iv)
        filled = [c for c in cs if c.strip()]
        if not filled:
            kinds.append(("empty", 0.0, 0, cs))
            continue
        n_date = sum(1 for c in filled if (d := find_date(c)) and c.startswith(d) and len(c) <= len(d) + 9)
        n_money = sum(1 for c in filled if looks_like_money(c) and not re.search(r"[A-Za-z]{3,}", c))
        n_dash = sum(1 for c in filled if c.strip() in _DASHES)
        letters = sum(ch.isalpha() for c in filled for ch in c)
        if n_date >= 0.7 * len(filled):
            kinds.append(("date", 0, letters, cs))
        elif n_money and n_money + n_dash >= 0.9 * len(filled):
            kinds.append(("money", n_money / len(rows), letters, cs))
        elif n_dash >= 0.9 * len(filled):
            kinds.append(("dash", 0, letters, cs))
        else:
            kinds.append(("text", 0, letters, cs))

    money = [i for i, k in enumerate(kinds) if k[0] == "money"]
    if not money:
        return []
    # a debit/credit column can be all "-" in the rows sampled: placeholders after the first amount column are money
    for i, k in enumerate(kinds):
        if k[0] == "dash" and i > money[0]:
            kinds[i] = ("money", 0.0, 0, k[3])
    money = [i for i, k in enumerate(kinds) if k[0] == "money"]

    def has_money(i: int) -> list[bool]:
        return [looks_like_money(c) for c in kinds[i][3]]

    def complementary(a: int, b: int) -> bool:
        pa, pb = has_money(a), has_money(b)
        any_ = [x or y for x, y in zip(pa, pb)]
        one = [x != y for x, y in zip(pa, pb)]
        return sum(any_) > 0 and sum(one) >= 0.9 * sum(any_)

    roles: dict[int, str] = {}
    if len(money) == 1:
        roles[money[0]] = "amount"
    elif len(money) == 2:
        a, b = money
        if complementary(a, b) and kinds[b][1] < 0.9:
            roles[a], roles[b] = "debit", "credit"
        else:
            roles[a], roles[b] = "amount", "balance"
    else:
        roles[money[-1]] = "balance"
        rest = money[:-1]
        if len(rest) >= 2 and complementary(rest[-2], rest[-1]):
            roles[rest[-2]], roles[rest[-1]] = "debit", "credit"
            extra = rest[:-2]
        else:
            roles[rest[-1]] = "amount"
            extra = rest[:-1]
        for i in extra:
            roles[i] = "other"
    dates = [i for i, k in enumerate(kinds) if k[0] == "date"]
    for n, i in enumerate(dates):
        roles[i] = "date" if n == 0 else ("value_date" if n == 1 else "other")
    texts = [i for i, k in enumerate(kinds) if k[0] == "text"]
    if texts:
        desc = max(texts, key=lambda i: kinds[i][2])
        roles[desc] = "description"
    for i, k in enumerate(kinds):
        if i not in roles:
            roles[i] = "reference" if "reference" not in roles.values() and k[0] in ("text", "dash") else "other"
    if "date" not in roles.values():
        return []
    return [((float(iv[0]), float(iv[1])), roles[i]) for i, iv in enumerate(intervals)]


def geometry_key(spec: LayoutSpec) -> Optional[str]:
    """Cache key for a header-less layout."""
    if not spec.column_spans:
        return None
    import hashlib

    # Role sequence only: exact positions drift between statements of the same layout, and a
    # collision between banks is harmless — every cached layout must still verify to be used.
    return "geo-" + hashlib.sha1("|".join(spec.roles()).encode()).hexdigest()[:12]


def find_headers(doc: Document, spec: LayoutSpec) -> list[HeaderHit]:
    headers = [c.header for c in spec.columns]
    want = set().union(*(tokens(h) for h in headers)) if headers else set()
    if not want:
        return []

    roles = spec.roles()

    def by_header_text(block: list[Line]):
        return align_spans(block, headers, roles)

    hits = _scan(doc, spec, want, by_header_text, 0.6)
    if hits or not spec.header_line:
        return hits

    # Fallback: the header strings did not match (the model paraphrased them), but it
    # pointed at the header line. Take that line's own cells as the columns, in order.
    ln = doc.line(spec.header_line)
    if ln is None or _has_money(ln) or date_prefix(ln.text):
        return []
    page = doc.pages[ln.page - 1]
    ncols = len(headers)
    block = _absorb(page.lines, ln.index, [ln], lambda n: len(n.cells()) <= ncols)

    by_cells = by_header_text
    first = by_cells(block)
    if not _usable(spec, first):
        return []
    # find the same header repeated on other pages by its actual printed words
    want2 = set().union(*(tokens(b.text) for b in block))
    hits = _scan(doc, spec, want2, by_cells, 0.9)
    if not any(h.line_ids[0] == ln.id for h in hits):
        hits.append(HeaderHit(ln.page, [b.id for b in block], block[-1].index, first))
        hits.sort(key=lambda h: (h.page, h.last_index))
    return hits


# ---------------------------------------------------------------------------
# Column boundaries
# ---------------------------------------------------------------------------


_LEFT_ALIGNED_ROLES = {"date", "value_date", "description", "reference", "dr_cr"}
_NUMERIC_WORD = re.compile(r"^[-+(]?[\d,]+(?:\.\d+)?\)?(?:Cr|Dr|CR|DR)?$")


def _free_runs(occupancy: dict[int, int], limit: int, lo: float, hi: float) -> list[tuple[int, int]]:
    runs, start = [], None
    for x in range(int(lo), int(hi) + 2):
        free = occupancy.get(x, 0) < limit and x <= hi
        if free and start is None:
            start = x
        elif not free and start is not None:
            runs.append((start, x - 1))
            start = None
    return runs


def column_boundaries(
    spans: list[Optional[tuple[float, float]]], data_lines: list[Line], roles: Optional[list[str]] = None
) -> tuple[list[tuple[int, float, float]], list["Zone"]]:
    """Return [(column_index, left, right)] territories for the columns that have anchors.

    A header always sits inside its own column, so the boundary between columns a and b
    lies in the whitespace between a's header end and b's header start. When that span
    holds more than one gutter, alignment decides: numbers are right-aligned, so after
    a numeric column the boundary is the right-most gutter (the data hugs the column's
    right edge); after a text column it is the left-most gutter (just past its text).
    """
    anchored = sorted(((i, s) for i, s in enumerate(spans) if s), key=lambda a: a[1][0])
    if not anchored:
        return [], []
    money = [ln for ln in data_lines if _has_money(ln) and len(ln.words) >= 2]
    # measure gutters on transaction-looking lines only: a summary or footer line spanning
    # the page would otherwise block a gutter on a page with few rows
    rows = [ln for ln in money if find_date(ln.text)] or money
    if len(rows) < 3:
        rows = money
    occupancy: dict[int, int] = {}
    for ln in rows:
        for w in ln.words:
            for x in range(int(w.x0), int(w.x1) + 1):
                occupancy[x] = occupancy.get(x, 0) + 1
    limit = max(1, int(0.05 * len(rows)))
    # a gutter must be wider than the space between words inside a cell ("23 Jan 2024")
    gaps = sorted(
        b.x0 - a.x1
        for ln in rows
        for a, b in zip(ln.words, ln.words[1:])
        if 0 < b.x0 - a.x1 < 0.6 * max(ln.y1 - ln.y0, 1.0)
    )
    space = gaps[len(gaps) // 2] if gaps else 2.0
    min_run = max(2, int(round(1.5 * space)))

    def wide(runs: list[tuple[int, int]]) -> list[tuple[int, int]]:
        return [r for r in runs if r[1] - r[0] + 1 >= min_run]

    def numeric(k: int) -> bool:
        i, (x0, _x1) = anchored[k]
        role = roles[i] if roles and i < len(roles) else "other"
        if role in MONEY_ROLES:
            return True
        if role in _LEFT_ALIGNED_ROLES:
            return False
        nxt = anchored[k + 1][1][0] if k + 1 < len(anchored) else float("inf")
        words = [w for ln in rows for w in ln.words if x0 - 10 <= w.xc < nxt]
        return bool(words) and sum(bool(_NUMERIC_WORD.match(w.text)) for w in words) > len(words) / 2

    cuts: list[float] = []
    zones: list[Zone] = []
    for k in range(len(anchored) - 1):
        a, b = anchored[k][1], anchored[k + 1][1]
        runs = []
        # right-aligned data of a numeric column starts well right of its header's left edge
        hi = (b[0] + b[1]) / 2 if numeric(k + 1) else b[0] + 2
        if rows and a[1] < b[0]:
            runs = wide(_free_runs(occupancy, limit, a[1], hi))
        if rows and not runs:
            runs = wide(_free_runs(occupancy, limit, (a[0] + a[1]) / 2, (b[0] + b[1]) / 2))
        if runs:
            num_a, num_b = numeric(k), numeric(k + 1)
            run = runs[-1] if num_a else runs[0]
            if not num_b:
                # left-aligned column on the right: cut just before its data starts, so
                # wrapped lines of the column on the left (not in `rows`) stay with it
                mid = (run[0] + run[1]) / 2
                starts = [w.x0 for ln in rows for w in ln.words if mid <= w.x0 <= run[1] + 2]
                cuts.append((min(starts) if starts else run[1] + 1) - 1)
            elif num_a:
                cuts.append(run[0] + 1)  # right-aligned column on the left: cut just after it
            else:
                cuts.append((run[0] + run[1]) / 2)
            if num_a != num_b:
                # Sparse strip between a text and a number column: a rare long narration
                # or a rare long amount can reach into it. Decide those words by content.
                ia, ib = anchored[k][0], anchored[k + 1][0]
                zones.append(Zone(run[0], run[1] + 1, money_col=ia if num_a else ib, text_col=ib if num_a else ia))
        else:
            cuts.append((a[1] + b[0]) / 2 if a[1] < b[0] else ((a[0] + a[1]) / 2 + (b[0] + b[1]) / 2) / 2)
    out = []
    edges = [float("-inf")] + cuts + [float("inf")]
    for k, (i, _s) in enumerate(anchored):
        out.append((i, edges[k], edges[k + 1]))
    return out, zones


@dataclass
class Zone:
    lo: float
    hi: float
    money_col: int
    text_col: int


def assign(line: Line, territories: list[tuple[int, float, float]], zones: list[Zone] | None = None) -> dict[int, list[Word]]:
    cols: dict[int, list[Word]] = {}
    for w in line.words:
        zone = next((z for z in zones or [] if z.lo <= w.xc < z.hi), None)
        if zone is not None:
            cols.setdefault(zone.money_col if looks_like_money(w.text) else zone.text_col, []).append(w)
            continue
        for i, left, right in territories:
            if left <= w.xc < right:
                cols.setdefault(i, []).append(w)
                break
    return cols


# ---------------------------------------------------------------------------
# Row segmentation
# ---------------------------------------------------------------------------


@dataclass
class _Row:
    lines: list[Line]
    cols: dict[int, list[tuple]] = field(default_factory=dict)  # column -> [((page, y), text)] per line
    is_anchor: bool = False

    def add(self, ln: Line, cols: dict[int, list[Word]]):
        self.lines.append(ln)
        for i, ws in cols.items():
            self.cols.setdefault(i, []).append(((ln.page, ln.y0), " ".join(w.text for w in ws)))

    def _frags(self, i: Optional[int]) -> list[str]:
        if i is None:
            return []
        # top-to-bottom, whatever order the lines were attached in (middle-aligned rows)
        return [t for _pos, t in sorted(self.cols.get(i, []), key=lambda f: f[0]) if t.strip()]

    def text(self, i: Optional[int]) -> str:
        return " ".join(self._frags(i)).strip()

    def first_text(self, i: Optional[int]) -> str:
        frags = self._frags(i)
        return frags[0].strip() if frags else ""


@dataclass
class TableResult:
    transactions: list[Transaction]
    anchors: list[Anchor]
    unaccounted: list[UnaccountedLine]
    outside_money_lines: list[UnaccountedLine]
    date_format: Optional[str]
    header_hits: list[HeaderHit]
    territories: dict[int, list[tuple[int, float, float]]]  # page -> territories
    consumed_line_ids: set[str]
    printed_headers: list[Optional[str]] = field(default_factory=list)  # per spec column, as printed
    notes: list[str] = field(default_factory=list)


def _bbox(lines: list[Line]) -> tuple[float, float, float, float]:
    return (min(l.x0 for l in lines), min(l.y0 for l in lines), max(l.x1 for l in lines), max(l.y1 for l in lines))


def _skip_kind(desc: str, markers: list[str]) -> Optional[str]:
    """If a row's narration is a non-transaction marker (B/F, totals...), return its kind."""
    d = re.sub(r"\s+", " ", desc.lower()).strip(" :-*")
    if not d:
        return None
    for m in markers:
        m = m.lower().strip()
        if not m:
            continue
        if d == m or d.startswith(m + " ") or d.startswith(m + ":") or (len(m) > 4 and d.startswith(m)):
            if "opening" in d or "brought" in d or "b/f" in d or d.startswith("bf") or "balance forward" in d:
                return "opening"
            if "closing" in d or "carried" in d or "c/f" in d:
                return "closing"
            return "total"
    return None


def apply_spec(doc: Document, spec: LayoutSpec) -> TableResult:
    from .models import ColumnSpec

    hits: list[HeaderHit] = [] if spec.column_spans else find_headers(doc, spec)
    if spec.column_spans:
        # replayed header-less layout: re-measure the columns on this statement when they agree
        fresh = columns_from_data(doc)
        if fresh and [r for _s, r in fresh] == spec.roles():
            spec.column_spans = [span for span, _r in fresh]
    if not hits and not spec.column_spans:
        # no readable header (image/banner header, or none at all): columns from the rows
        derived = columns_from_data(doc)
        if derived:
            spec.columns = [ColumnSpec(f"({role})", role) for _span, role in derived]
            spec.column_spans = [span for span, _role in derived]
    roles = spec.roles()
    idx = {r: roles.index(r) for r in set(roles)}  # first column with each role
    date_i = idx.get("date")
    desc_cols = [i for i, r in enumerate(roles) if r == "description"]
    ref_i = idx.get("reference")
    vdate_i = idx.get("value_date")
    money_cols = {i for i, r in enumerate(roles) if r in MONEY_ROLES}
    amount_cols = {i for i, r in enumerate(roles) if r in AMOUNT_ROLES}
    markers = DEFAULT_SKIP_MARKERS + [m.lower() for m in spec.non_transaction_rows]

    n_extra = max((len(h.spans) - len(roles) for h in hits), default=0)
    roles = roles + ["other"] * n_extra  # printed columns the layout spec did not name
    hits_by_page: dict[int, list[HeaderHit]] = {}
    for h in hits:
        hits_by_page.setdefault(h.page, []).append(h)

    # ---- pass 1: per page, compute territories and per-line column assignment
    page_lines: list[tuple[Line, dict[int, list[Word]]]] = []
    territories_by_page: dict[int, list[tuple[int, float, float]]] = {}
    header_line_ids: set[str] = set()
    active_spans = None
    table_start = find_table_start(doc) if spec.column_spans else None
    for page in doc.pages:
        ph = hits_by_page.get(page.number, [])
        for h in ph:
            header_line_ids.update(h.line_ids)
        if spec.column_spans:
            if table_start is None or page.number < table_start.page:
                continue
            active_spans = [tuple(sp) for sp in spec.column_spans]
            # a narration's first line can sit just above the first dated row
            start = max(0, table_start.index - 2) if page.number == table_start.page else 0
        elif ph:
            active_spans = ph[0].spans
            start = ph[0].last_index + 1
        elif active_spans is not None:
            start = 0
        else:
            continue
        region = [ln for ln in page.lines[start:] if ln.id not in header_line_ids]
        terr, zones = column_boundaries(active_spans, region, roles)
        territories_by_page[page.number] = terr
        for ln in region:
            page_lines.append((ln, assign(ln, terr, zones)))

    # ---- date format from the date column
    def col_text(cols, i):
        return " ".join(w.text for w in cols.get(i, [])) if i is not None else ""

    def split_date_text(k: int) -> str:
        """Date-column text of line k joined with the line directly below it — some banks
        print "12 Apr" on the row's line and "2024" underneath."""
        ln, cols = page_lines[k]
        if k + 1 < len(page_lines):
            nxt, ncols = page_lines[k + 1]
            if nxt.page == ln.page and nxt.y0 - ln.y1 < 0.8 * _line_height(ln):
                return (col_text(cols, date_i) + " " + col_text(ncols, date_i)).strip()
        return ""

    samples = [find_date(col_text(c, date_i)) or find_date(split_date_text(k)) for k, (_, c) in enumerate(page_lines)]
    date_fmt = spec.date_format or infer_date_format([s for s in samples if s])
    if spec.date_format and samples:
        ok = sum(1 for s in samples if s and parse_date(s, spec.date_format))
        alt = infer_date_format([s for s in samples if s])
        if alt and alt != spec.date_format:
            ok_alt = sum(1 for s in samples if s and parse_date(s, alt))
            if ok_alt > ok:
                date_fmt = alt

    def line_date(cols) -> Optional[str]:
        p = find_date(col_text(cols, date_i), date_fmt)
        return p

    text_roles = {"description", "reference", "other", "dr_cr"}
    _time_like = re.compile(r"^[\d:/\-.]+$|^(?:AM|PM|am|pm)$")

    def continuation_ok(cols) -> bool:
        """A wrapped-narration line only has words in text columns (a time is allowed under a date)."""
        for i, ws in cols.items():
            role = roles[i]
            if role in ("date", "value_date"):
                if not all(_time_like.match(w.text) for w in ws):
                    return False
            elif role not in text_roles:
                return False
        return True

    # ---- pass 2: classify lines
    kinds: list[str] = []  # start | money | text | free (free text that is not a continuation)
    for k, (ln, cols) in enumerate(page_lines):
        has_date = line_date(cols) is not None
        if not has_date and col_text(cols, date_i):
            joined = split_date_text(k)
            p = find_date(joined, date_fmt)
            # only a genuine split: the line alone has no date, the pair does, and the date
            # starts on this line (not entirely on the next one)
            has_date = bool(p and not find_date(col_text(page_lines[k + 1][1], date_i), date_fmt))
        money_in_amount = any(any(looks_like_money(w.text) for w in cols.get(i, [])) for i in amount_cols)
        money_any = any(any(looks_like_money(w.text) for w in cols.get(i, [])) for i in money_cols)
        if has_date:
            kinds.append("start")
        elif money_any:
            kinds.append("money")  # no date, but money in a money column
        elif continuation_ok(cols):
            kinds.append("text")
        else:
            kinds.append("free")  # footer/notes/section titles spanning columns

    # ---- narration sanity: the description column must hold words. If the layout put a
    # reference/cheque column there and left the real narration unclaimed (the model skipped
    # a column), the data says which column is which — swap them.
    notes: list[str] = []

    def letter_share(i: int) -> tuple[float, int]:
        txt = "".join(w.text for k, (_ln, cols) in enumerate(page_lines) if kinds[k] == "start" for w in cols.get(i, []))
        total = len(txt)
        return (sum(ch.isalpha() for ch in txt) / total if total else 0.0), total

    n_starts = sum(1 for k in kinds if k == "start")

    # a "description" column whose cells are bare dates is a value/posting-date column
    def mostly_dates(i: int) -> bool:
        cells = [col_text(cols, i).strip() for k, (_ln, cols) in enumerate(page_lines) if kinds[k] == "start"]
        cells = [c for c in cells if c]
        return bool(cells) and sum(1 for c in cells if (d := find_date(c)) and c.startswith(d) and len(c) <= len(d) + 9) >= 0.8 * len(cells)

    for i in list(desc_cols):
        if len(desc_cols) > 1 and mostly_dates(i):
            roles[i] = "value_date" if "value_date" not in roles else "other"
            desc_cols.remove(i)
            notes.append("A column named as narration held dates; it was read as the value date.")
    idx = {r: roles.index(r) for r in set(roles)}
    vdate_i = idx.get("value_date")

    desc_share = max((letter_share(i)[0] for i in desc_cols), default=0.0)
    if n_starts and desc_share < 0.35:
        cands = [i for i, r in enumerate(roles) if r in ("other", "reference") and i not in desc_cols]
        scored = [(letter_share(i), i) for i in cands]
        scored = [((sh, tot), i) for (sh, tot), i in scored if sh > 0.5 and tot >= 4 * n_starts]
        if scored:
            _, best = max(scored, key=lambda x: x[0][0] * x[0][1])
            for i in desc_cols:
                roles[i] = "reference"
            roles[best] = "description"
            idx = {r: roles.index(r) for r in set(roles)}
            desc_cols = [best]
            ref_i = idx.get("reference")
            notes.append("The column named as the narration held numbers; the narration was read from the column that holds words.")

    # ---- pass 3: group into rows (per page so geometry is comparable)
    rows: list[_Row] = []
    unaccounted: list[UnaccountedLine] = []
    outside: list[UnaccountedLine] = []
    consumed: set[str] = set()

    n = len(page_lines)
    pos = 0
    while pos < n:
        page_no = page_lines[pos][0].page
        end = pos
        while end < n and page_lines[end][0].page == page_no:
            end += 1
        seg = list(range(pos, end))
        pos = end
        starts = [k for k in seg if kinds[k] == "start"]
        heights = [_line_height(page_lines[k][0]) for k in seg]
        h = median(heights) if heights else 8.0
        if not starts:
            # A page with no dated rows: only carry-forward style lines matter.
            for k in seg:
                ln, cols = page_lines[k]
                if kinds[k] == "money":
                    desc = " ".join(col_text(cols, i) for i in desc_cols) or ln.text
                    if _skip_kind(desc, markers) or _skip_kind(ln.text, markers):
                        r = _Row([], is_anchor=True)
                        r.add(ln, {i: ws for i, ws in cols.items()})
                        rows.append(r)
                        consumed.add(ln.id)
                    else:
                        outside.append(UnaccountedLine(ln.id, ln.page, ln.text, "money outside the transaction table", _bbox([ln])))
            continue

        first_start, last_start = starts[0], starts[-1]
        # middle/bottom-aligned rows: text lines sit just above the first dated row
        desc_above = any(
            kinds[k] == "text"
            and page_lines[first_start][0].y0 - page_lines[k][0].y1 < 0.8 * h
            for k in seg
            if k < first_start
        )
        # table end on this page: first line after the last start that is far away
        table_end = end
        prev_bottom = page_lines[last_start][0].y1
        for k in range(last_start + 1, end):
            ln = page_lines[k][0]
            if kinds[k] == "start":
                prev_bottom = ln.y1
                continue
            if ln.y0 - prev_bottom > 1.6 * h or kinds[k] == "free":
                table_end = k
                break
            prev_bottom = ln.y1

        start_rows: dict[int, _Row] = {}
        for k in starts:
            r = _Row([])
            r.add(*page_lines[k])
            start_rows[k] = r
            consumed.add(page_lines[k][0].id)

        def nearest_start(k: int) -> Optional[int]:
            prev = max((s for s in starts if s < k), default=None)
            nxt = min((s for s in starts if s > k), default=None)
            ln = page_lines[k][0]
            if prev is None:
                if nxt is not None and page_lines[nxt][0].y0 - ln.y1 < 0.8 * h:
                    return nxt
                return None
            # distance to previous row = gap to the line directly above
            above = page_lines[k - 1][0]
            gap_above = ln.y0 - above.y1
            if nxt is None:
                return prev
            gap_below = page_lines[nxt][0].y0 - ln.y1
            if desc_above:
                # with uniform spacing a tie goes to the next row in middle-aligned layouts
                if gap_below < gap_above - 0.15 * h or (abs(gap_below - gap_above) <= 0.15 * h and k + 1 == nxt):
                    return nxt
                return prev
            if gap_below < gap_above - 0.5 * h:
                return nxt
            return prev

        for k in seg:
            if kinds[k] == "start":
                continue
            ln, cols = page_lines[k]
            desc = " ".join(col_text(cols, i) for i in desc_cols).strip()
            in_table = (k > first_start or (desc_above and k < first_start)) and k < table_end
            if kinds[k] == "money":
                kind = _skip_kind(desc, markers) or _skip_kind(ln.text, markers)
                if kind and k < table_end:  # B/F above the rows, C/F/Total right after them
                    r = _Row([], is_anchor=True)
                    r.add(ln, cols)
                    rows.append(r) if k < first_start else start_rows.setdefault(k, r)
                    consumed.add(ln.id)
                    continue
                if not in_table and k < first_start:
                    outside.append(UnaccountedLine(ln.id, ln.page, ln.text, "money above the transaction table", _bbox([ln])))
                    continue
                if k >= table_end:
                    outside.append(UnaccountedLine(ln.id, ln.page, ln.text, "money below the transaction table", _bbox([ln])))
                    continue
                target = nearest_start(k)
                tr = start_rows.get(target) if target is not None else None
                line_money_cols = [i for i in money_cols if any(looks_like_money(w.text) for w in cols.get(i, []))]
                missing = tr is not None and all(not tr.text(i) for i in line_money_cols)
                if tr is not None and missing and not tr.is_anchor:
                    tr.add(ln, cols)
                    consumed.add(ln.id)
                else:
                    unaccounted.append(UnaccountedLine(ln.id, ln.page, ln.text, "amount on a line with no date that could not be attached to a transaction", _bbox([ln])))
                continue
            # plain text line
            if not in_table or kinds[k] == "free":
                continue
            target = nearest_start(k)
            if target is not None and target in start_rows and not start_rows[target].is_anchor:
                start_rows[target].add(ln, cols)
                consumed.add(ln.id)

        for k in sorted(start_rows):
            rows.append(start_rows[k])

    # rows were appended page by page, but anchors found before a page's first start were
    # appended before that page's start rows — order rows by position in the document.
    order = {ln.id: i for i, (ln, _c) in enumerate(page_lines)}
    rows.sort(key=lambda r: min(order[l.id] for l in r.lines))

    # ---- pass 4: typed values
    txns: list[Transaction] = []
    anchors: list[Anchor] = []
    cc = spec.statement_type == "credit_card"
    for r in rows:
        r.lines.sort(key=lambda l: (l.page, l.y0))
        desc = " ".join(r.text(i) for i in desc_cols).strip()
        bal_txt = r.first_text(idx.get("balance"))
        bal_p = parse_amount(bal_txt) if bal_txt else None
        balance = signed_balance(bal_p) if bal_p else None
        date_txt = find_date(r.first_text(date_i), date_fmt) or find_date(r.text(date_i), date_fmt) or ""
        anchor_kind = _skip_kind(desc, markers) or (
            _skip_kind(" ".join(l.text for l in r.lines), markers) if not desc else None
        )
        if r.is_anchor or anchor_kind:
            # Distinguish a real transaction whose narration merely starts with "Total..."
            debit_t = r.first_text(idx.get("debit"))
            credit_t = r.first_text(idx.get("credit"))
            amount_t = r.first_text(idx.get("amount"))
            has_dc = bool(parse_amount(debit_t)) ^ bool(parse_amount(credit_t))
            exact = re.sub(r"[^a-z/ ]", "", desc.lower()).strip() in [m.strip() for m in markers]
            if r.is_anchor or exact or not (has_dc or parse_amount(amount_t)):
                dp, cp = parse_amount(debit_t), parse_amount(credit_t)
                anchors.append(
                    Anchor(
                        kind=anchor_kind or "carry",
                        balance=balance,
                        text=" ".join(l.text for l in r.lines),
                        line_id=r.lines[0].id,
                        after_txn=txns[-1].id if txns else None,
                        debit_total=dp.value if dp else None,
                        credit_total=cp.value if cp else None,
                    )
                )
                continue

        amount, direction, src, amount_text = _amount_and_direction(r, roles, idx, cc)
        d = parse_date(date_txt, date_fmt) if date_txt and date_fmt else None
        vd_txt = (find_date(r.first_text(vdate_i)) or find_date(r.text(vdate_i))) if vdate_i is not None else None
        vd = None
        if vd_txt:
            vd = parse_date(vd_txt, date_fmt) or None
        # the date column may hold "01/04/2024 10:32:11" or narration spill-over
        full_date_col = r.text(date_i)
        cut = full_date_col.find(date_txt) if date_txt else -1
        date_col_rest = (full_date_col[cut + len(date_txt):].strip() if cut >= 0 else "") if date_i is not None else ""
        if date_col_rest and not re.fullmatch(r"[\d:/\-\sAPMapm.]+", date_col_rest):
            desc = (date_col_rest + " " + desc).strip()
        txns.append(
            Transaction(
                id=len(txns) + 1,
                date=d,
                description=re.sub(r"\s+", " ", desc),
                amount=amount,
                direction=direction,
                balance=balance,
                reference=r.text(ref_i) or None,
                value_date=vd,
                date_text=date_txt,
                amount_text=amount_text,
                balance_text=bal_txt,
                direction_source=src,
                source=SourceRef(r.lines[0].page, [l.id for l in r.lines], _bbox([l for l in r.lines if l.page == r.lines[0].page])),
            )
        )

    resolve_directions(txns, anchors)
    printed: list[Optional[str]] = [None] * len(roles)
    if hits:
        words = [w for lid in hits[0].line_ids for w in (doc.line(lid).words if doc.line(lid) else [])]
        for k, span in enumerate(hits[0].spans):
            if span:
                inside = sorted((w for w in words if span[0] - 1 <= w.xc <= span[1] + 1), key=lambda w: (w.y0, w.x0))
                printed[k] = " ".join(w.text for w in inside) or None
    return TableResult(txns, anchors, unaccounted, outside, date_fmt, hits, territories_by_page, consumed, printed, notes)


def _amount_and_direction(r: _Row, roles: list[str], idx: dict, cc: bool):
    """Return (amount, direction, direction_source, amount_text)."""
    if "debit" in idx or "credit" in idx:
        dt = r.first_text(idx.get("debit"))
        ct = r.first_text(idx.get("credit"))
        dp, cp = parse_amount(dt), parse_amount(ct)
        dv = dp.value if dp else Decimal(0)
        cv = cp.value if cp else Decimal(0)
        if dv and not cv:
            # a credit-card "debit" column can still carry a Cr-marked refund
            if dp.marker == "cr":
                return dv, "credit", "marker", dt
            return dv, "debit", "column", dt
        if cv and not dv:
            return cv, "credit", "column", ct
        if dv and cv:
            return None, None, "ambiguous", f"{dt} / {ct}"
        # both empty: maybe a single amount column exists as well
        if "amount" not in idx:
            return None, None, "missing", ""
    at = r.first_text(idx.get("amount"))
    ap = parse_amount(at)
    if not ap:
        return None, None, "missing", at
    if "dr_cr" in idx:
        flag = r.first_text(idx["dr_cr"]).strip().lower().rstrip(".")
        if flag in ("cr", "c", "credit", "+"):
            return ap.value, "credit", "column", at
        if flag in ("dr", "d", "debit", "-"):
            return ap.value, "debit", "column", at
    return (*_direction_from_marker(ap, cc), at)


def _direction_from_marker(ap: ParsedAmount, cc: bool):
    if ap.marker == "cr":
        return ap.value, "credit", "marker"
    if ap.marker == "dr":
        return ap.value, "debit", "marker"
    if ap.negative:
        return ap.value, ("credit" if cc else "debit"), "sign"
    if cc:
        return ap.value, "debit", "default"
    return ap.value, None, "unresolved"


def resolve_directions(txns: list[Transaction], anchors: list[Anchor], opening: Optional[Decimal] = None) -> None:
    """Bank statements with an unsigned single amount column: use the balance movement.

    Called once from apply_spec (in-table opening rows only) and again by the pipeline
    once the printed opening balance from the summary is known.
    """
    unresolved = [t for t in txns if t.direction is None and t.amount is not None]
    if not unresolved:
        return
    seq = list(reversed(txns)) if _is_newest_first(txns) else txns
    if opening is None:
        opening = next((a.balance for a in anchors if a.kind == "opening" and a.balance is not None), None)
    prev = opening
    for t in seq:
        if t.direction is None and t.amount is not None and prev is not None and t.balance is not None:
            delta = t.balance - prev
            if delta == t.amount:
                t.direction, t.direction_source = "credit", "balance_delta"
            elif delta == -t.amount:
                t.direction, t.direction_source = "debit", "balance_delta"
        if t.balance is not None:
            prev = t.balance
        elif prev is not None and t.direction and t.amount is not None:
            prev = prev + (t.amount if t.direction == "credit" else -t.amount)
        else:
            prev = None


def _is_newest_first(txns: list[Transaction]) -> bool:
    dated = [t.date for t in txns if t.date]
    if len(dated) < 2:
        return False
    inc = sum(1 for a, b in zip(dated, dated[1:]) if b > a)
    dec = sum(1 for a, b in zip(dated, dated[1:]) if b < a)
    return dec > inc
