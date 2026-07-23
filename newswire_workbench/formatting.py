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
            and 45 <= len(p.get_text(" ", strip=True)) <= 420
        ]
        needed = target - current
        for index in sorted(_even_indices(len(paragraphs), min(needed, len(paragraphs)))):
            paragraph = paragraphs[index]
            text_node = next((n for n in paragraph.descendants
                              if isinstance(n, NavigableString) and len(str(n).strip()) >= 35), None)
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
    return str(soup)
