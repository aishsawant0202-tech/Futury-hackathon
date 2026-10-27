"""Check that a quote really stands on the page it claims. This is the guard against invented rules.

Plain code, no model: the extraction agent's output is only trusted after this check.

PDF text is messy, so both sides are normalised first: words split at a line break are
joined again ("Arbeitsunfähigkeitsbescheini-\\ngung"), quote marks and dashes are unified,
bullets and repeated spaces disappear.

A quote is verified only if it appears exactly, ignoring spaces and hyphens (PDF text scatters
spaces like "K lärung", and models copy line-break hyphens like "Ar-beitsunfähigkeiten").
Fuzzy matching is not enough on its own: "eine" vs "keine" scores 99 but flips the rule.
The RapidFuzz score only marks near misses for the examiner to look at.
"""
import re
import sqlite3
from dataclasses import dataclass

from rapidfuzz import fuzz

from specpilot import db
from specpilot.models import Requirement

CLOSE = 85.0  # not exact, but this similar: probably the right place, examiner should compare

_HYPHEN_BREAK = re.compile(r"(\w)-\s*\n\s*(\w)")
_CHARS = str.maketrans({"„": '"', "“": '"', "”": '"', "‚": "'", "‘": "'", "’": "'", "–": "-", "—": "-", "•": " ", " ": " "})
_SPACES = re.compile(r"\s+")


@dataclass
class QuoteResult:
    page: int
    verified: bool  # appears exactly on the page
    score: float  # 0-100, how well the best stretch of the page matches
    match: str  # that stretch, normalised, for the examiner to compare


def normalize(text: str) -> str:
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    text = text.translate(_CHARS)
    return _SPACES.sub(" ", text).strip().lower()


def compact(text: str) -> str:
    """Normalised text without spaces and hyphens: what an exact match compares."""
    return normalize(text).replace(" ", "").replace("-", "")


def compare(quote: str, text: str) -> tuple[bool, float, str]:
    """(exact, score 0-100, best matching stretch of text)."""
    q, t = normalize(quote), normalize(text)
    if not q or not t:
        return False, 0.0, ""
    if compact(quote) in compact(text):
        return True, 100.0, q
    m = fuzz.partial_ratio_alignment(q, t)
    return False, m.score, t[m.dest_start : m.dest_end]


def check(conn: sqlite3.Connection, document: str, quote: str, page: int) -> QuoteResult:
    text = db.get_page(conn, document, page) or ""
    return QuoteResult(page, *compare(quote, text))


def locate(conn: sqlite3.Connection, document: str, quote: str, pages: list[int] | None = None) -> QuoteResult | None:
    """Find the page where the quote fits best. Used when it is not on the page the model named."""
    if pages is None:
        pages = [r["page"] for r in conn.execute("SELECT page FROM spec_pages WHERE document = ? ORDER BY page", (document,))]
    best = None
    for p in pages:
        r = check(conn, document, quote, p)
        if r.verified:
            return r
        if best is None or r.score > best.score:
            best = r
    return best


def verify_requirement(conn: sqlite3.Connection, r: Requirement) -> Requirement:
    """Set quote_verified and add a flag when the quote is only close, on another page, or nowhere."""
    result = check(conn, r.document, r.quote, r.page)
    if result.verified:
        return r.model_copy(update={"quote_verified": True})

    flags = list(r.flags)
    if result.score >= CLOSE:
        flags.append(f"Quote not exact on p.{r.page} (score {result.score:.0f}); page says: '{result.match}'")
    else:
        elsewhere = locate(conn, r.document, r.quote)
        if elsewhere and elsewhere.verified:
            flags.append(f"Quote is on p.{elsewhere.page}, not on p.{r.page}")
        else:
            flags.append(f"Quote not found in the spec (best: p.{elsewhere.page if elsewhere else '-'}, score {elsewhere.score if elsewhere else 0:.0f})")
    return r.model_copy(update={"quote_verified": False, "flags": flags})
