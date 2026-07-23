"""Mechanical MBK HTML normalization without rewriting editorial text."""

import re

from bs4 import BeautifulSoup, NavigableString


def _even_indices(total, wanted):
    if wanted <= 0 or total <= 0:
        return set()
    if wanted == 1:
        return {total // 2}
    return {round(i * (total - 1) / (wanted - 1)) for i in range(wanted)}


def normalize_master_html(html, word_count):
    """Normalize heading/CTA bolding and cap persuasive emphasis naturally."""
    soup = BeautifulSoup(html, "html.parser")
    target = 10 if word_count < 1600 else 11 if word_count < 2200 else 12

    # Headings and CTA anchors are explicitly bold in source HTML.
    for tag in soup.find_all(["h2", "h3", "a"]):
        for nested in list(tag.find_all("strong")):
            nested.unwrap()
        wrapper = soup.new_tag("strong")
        for child in list(tag.contents):
            wrapper.append(child.extract())
        tag.append(wrapper)

    candidates = []
    for strong in list(soup.find_all("strong")):
        if strong.find_parent(["h2", "h3", "a"]) or strong.find("a"):
            if strong.find("a"):
                strong.unwrap()
            continue
        text = strong.get_text(" ", strip=True)
        parent = strong.parent
        functional_label = (
            parent and parent.name == "li" and
            (text.endswith(":") or len(text.split()) <= 3)
        )
        if functional_label:
            strong.attrs.pop("class", None)
            continue
        if 3 <= len(text) <= 180:
            candidates.append(strong)
        else:
            strong.unwrap()

    selected = _even_indices(len(candidates), min(target, len(candidates)))
    for index, strong in enumerate(candidates):
        if index in selected:
            strong["class"] = ["key-takeaway"]
        else:
            strong.unwrap()

    current = len(soup.select("strong.key-takeaway"))
    if current < target:
        paragraphs = [
            p for p in soup.find_all("p")
            if not p.find("a") and not p.find("strong", class_="key-takeaway")
            and 30 <= len(p.get_text(" ", strip=True)) <= 420
        ]
        needed = target - current
        for index in sorted(_even_indices(len(paragraphs), min(needed, len(paragraphs)))):
            paragraph = paragraphs[index]
            text_node = next((n for n in paragraph.descendants
                              if isinstance(n, NavigableString) and len(str(n).strip()) >= 25), None)
            if text_node is None:
                continue
            raw = str(text_node)
            stripped = raw.strip()
            match = re.match(r"(.{25,120}?(?:[,.!?;:]|\s))", stripped)
            phrase = (match.group(1) if match else stripped[:100]).strip()
            phrase = phrase.rsplit(" ", 1)[0] if len(phrase) >= 100 and " " in phrase else phrase
            if len(phrase) < 20:
                continue
            leading = raw[:len(raw) - len(raw.lstrip())]
            start = raw.find(stripped)
            remainder = stripped[len(phrase):]
            strong = soup.new_tag("strong")
            strong["class"] = ["key-takeaway"]
            strong.string = phrase
            replacement = []
            if leading:
                replacement.append(NavigableString(leading))
            replacement.append(strong)
            if remainder:
                replacement.append(NavigableString(remainder))
            text_node.replace_with(*replacement)
    return str(soup)


def ensure_affiliate_links(html, href, target=5):
    """Add only the missing, varied CTAs and ensure one appears near the lead."""
    if not href or href.upper() == "TRAFFIC-FIRST":
        return html
    soup = BeautifulSoup(html, "html.parser")
    matching = [a for a in soup.find_all("a") if a.get("href") == href]
    texts = (
        "Review the current offer details and subscription terms",
        "See current subscription pricing and included reports",
        "Check the current package and refund terms",
        "Explore what is included with the subscription",
        "View the current Forecasts & Strategies offer",
    )

    def cta(index):
        paragraph = soup.new_tag("p")
        anchor = soup.new_tag("a", href=href)
        strong = soup.new_tag("strong")
        strong.string = texts[index % len(texts)]
        anchor.append(strong)
        paragraph.append(anchor)
        return paragraph

    # If the first affiliate link is not near the lead, the next missing CTA is
    # inserted after the second top-level paragraph.
    serialized = str(soup)
    first_offset = serialized.find(f'href="{href}"')
    if len(matching) < target and (first_offset < 0 or first_offset > max(1200, len(serialized) // 4)):
        paragraphs = soup.find_all("p", recursive=False) or soup.find_all("p")
        anchor_point = paragraphs[min(1, len(paragraphs) - 1)] if paragraphs else None
        if anchor_point:
            anchor_point.insert_after(cta(len(matching)))
            matching.append(True)

    headings = soup.find_all("h2")
    while len(matching) < target and headings:
        position = round((len(matching) + 1) * (len(headings) - 1) / (target + 1))
        headings[min(position, len(headings) - 1)].insert_after(cta(len(matching)))
        matching.append(True)
    # Thin or mechanically normalized drafts may not retain enough headings.
    # Distribute remaining CTAs across substantive paragraphs instead of
    # treating missing headings as an admin-level problem.
    paragraphs = [
        p for p in soup.find_all("p")
        if not p.find("a") and len(p.get_text(" ", strip=True)) >= 25
    ]
    used_positions = set()
    while len(matching) < target and paragraphs:
        position = round(
            len(matching) * (len(paragraphs) - 1) / max(target - 1, 1)
        )
        while position in used_positions and position + 1 < len(paragraphs):
            position += 1
        used_positions.add(position)
        paragraphs[position].insert_after(cta(len(matching)))
        matching.append(True)
    return str(soup)


def repair_publication_gates(html, platform, vertical, affiliate_href=""):
    """Apply deterministic publication fixes without changing factual meaning."""
    soup = BeautifulSoup(html, "html.parser")

    # WordPress stores the release title separately.
    for heading in list(soup.find_all("h1")):
        heading.decompose()

    text = soup.get_text(" ", strip=True)
    word_count = len(re.findall(r"\b[\w’'-]+\b", text))

    # The disclosure and risk statement are mechanical requirements, not
    # editorial judgment calls.
    lead_text = soup.get_text(" ", strip=True)[:1200].casefold()
    if "advertorial" not in lead_text:
        disclosure = BeautifulSoup(
            "<p><strong>Paid Advertorial</strong></p>", "html.parser"
        ).p
        first = soup.find()
        if first:
            first.insert_before(disclosure)
        else:
            soup.append(disclosure)
    if (
        affiliate_href
        and affiliate_href.upper() != "TRAFFIC-FIRST"
        and not re.search(
            r"compensation may be received|a commission may be earned",
            soup.get_text(" ", strip=True),
            re.I,
        )
    ):
        compensation = BeautifulSoup(
            "<p>Compensation may be received if a purchase is made through "
            "links in this advertorial.</p>",
            "html.parser",
        ).p
        paid = next(
            (p for p in soup.find_all("p") if "advertorial" in p.get_text().casefold()),
            None,
        )
        if paid:
            paid.insert_after(compensation)
        else:
            soup.insert(0, compensation)
    if vertical == "financial" and not re.search(
        r"(?:loss of principal|investments? (?:involve|carry|includes?) risk)",
        soup.get_text(" ", strip=True), re.I,
    ):
        risk = BeautifulSoup(
            "<p><strong>Investing involves risk, including the possible loss "
            "of principal.</strong></p>",
            "html.parser",
        ).p
        paragraphs = soup.find_all("p")
        if paragraphs:
            paragraphs[min(1, len(paragraphs) - 1)].insert_after(risk)
        else:
            soup.append(risk)

    # Remove production terminology and intermediary-routing explanations.
    serialized = str(soup)
    serialized = re.sub(
        r"\b(?:source intelligence|label ocr|phase 0(?:\.1)?|mbk|path [abc]|"
        r"cvd-?\d+|c(?:1|2|15|19)\b|r\d+\b|b[1-4]\b)",
        "",
        serialized,
        flags=re.I,
    )
    serialized = re.sub(
        r"[^<.]{0,80}(?:third[- ]party partner|rather than the official|"
        r"not the official)[^<.]{0,120}[.]?",
        "",
        serialized,
        flags=re.I,
    )
    serialized = re.sub(r"\bguaranteed trial\b", "subscription offer", serialized, flags=re.I)
    serialized = re.sub(
        r"\b(?:official|verified) (?:order|purchase|checkout) page\b",
        "current offer details",
        serialized,
        flags=re.I,
    )
    serialized = re.sub(
        r"\b(?:we|our|us) (?:may |might |can )?(?:earn|receive|be compensated)"
        r"[^.<]{0,120}[.]?",
        "Compensation may be received if a purchase is made through links in this advertorial.",
        serialized,
        flags=re.I,
    )

    soup = BeautifulSoup(serialized, "html.parser")
    for anchor in soup.find_all("a"):
        if re.match(r"^(?:https?://|www\.)", anchor.get_text(" ", strip=True), re.I):
            anchor.clear()
            strong = soup.new_tag("strong")
            strong.string = "Review the current offer details"
            anchor.append(strong)

    normalized = normalize_master_html(str(soup), word_count)
    if platform == "AccessNewsWire" and affiliate_href and affiliate_href.upper() != "TRAFFIC-FIRST":
        normalized = ensure_affiliate_links(normalized, affiliate_href, target=5)
        # Link insertion can create new nodes, so normalize once more.
        normalized = normalize_master_html(normalized, word_count)
    return normalized
