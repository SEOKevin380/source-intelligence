"""Zero-cost American-English and human-cadence diagnostics."""

from __future__ import annotations

import html
import re
from collections import Counter


_US_SPELLINGS = {
    "optimisation": "optimization",
    "optimise": "optimize",
    "colour": "color",
    "favour": "favor",
    "favourite": "favorite",
    "centre": "center",
    "labour": "labor",
    "behaviour": "behavior",
    "organisation": "organization",
}


def normalize_american_english(article: str) -> tuple[str, list[dict]]:
    """Apply spelling-only US English changes that cannot alter factual meaning."""
    changes = []
    result = article
    for british, american in _US_SPELLINGS.items():
        pattern = re.compile(rf"\b{re.escape(british)}\b", re.I)
        if pattern.search(result):
            result = pattern.sub(american, result)
            changes.append({"from": british, "to": american})
    return result, changes


def human_copy_diagnostics(article: str) -> dict:
    """Report likely robotic/repetitive prose without creating a new gate."""
    plain = html.unescape(re.sub(r"<[^>]+>", " ", article))
    plain = re.sub(r"\s+", " ", plain).strip()
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", plain)
        if sentence.strip()
    ]
    word_counts = [
        len(re.findall(r"\b[\w’'-]+\b", sentence))
        for sentence in sentences
    ]
    starters = Counter(
        " ".join(re.findall(r"[a-z0-9]+", sentence.casefold())[:2])
        for sentence in sentences
        if len(re.findall(r"[a-z0-9]+", sentence.casefold())) >= 2
    )
    repeated_starters = [
        {"text": text, "count": count}
        for text, count in starters.most_common()
        if count >= 3
    ]
    british = sorted({
        match.group(0).casefold()
        for spelling in _US_SPELLINGS
        for match in re.finditer(
            rf"\b{re.escape(spelling)}\b", plain, re.I
        )
    })
    paragraphs = [
        re.sub(r"<[^>]+>", " ", value)
        for value in re.findall(r"<p\b[^>]*>(.*?)</p>", article, re.I | re.S)
    ]
    paragraph_words = [
        len(re.findall(r"\b[\w’'-]+\b", value)) for value in paragraphs
    ]
    issues = []
    if any(count > 35 for count in word_counts):
        issues.append("long_sentences")
    if repeated_starters:
        issues.append("repeated_sentence_starters")
    if paragraph_words and max(paragraph_words) > 120:
        issues.append("oversized_paragraphs")
    if british:
        issues.append("non_american_spelling")
    return {
        "schema_version": 1,
        "sentence_count": len(sentences),
        "average_sentence_words": (
            round(sum(word_counts) / len(word_counts), 1)
            if word_counts else 0
        ),
        "long_sentence_count": sum(count > 35 for count in word_counts),
        "oversized_paragraph_count": sum(
            count > 120 for count in paragraph_words
        ),
        "repeated_starters": repeated_starters,
        "non_american_spellings": british,
        "issues": issues,
        "blocking": False,
    }
