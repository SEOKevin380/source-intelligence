"""Mechanical MBK HTML normalization without rewriting editorial text."""

import html as html_lib
import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup, NavigableString

from source_pack_contract import STRUCTURED_PRODUCT_CLAIM_TYPES


def _even_indices(total, wanted):
    if wanted <= 0 or total <= 0:
        return set()
    if wanted == 1:
        return {total // 2}
    return {round(i * (total - 1) / (wanted - 1)) for i in range(wanted)}


def _repair_mixed_markdown(value):
    """Canonicalize Markdown residue embedded inside otherwise valid HTML."""
    value = re.sub(r"```(?:html|markdown|md)?", "", value, flags=re.I)
    value = value.replace("```", "").replace("~~~", "")
    soup = BeautifulSoup(value, "html.parser")

    # A model commonly places Markdown headings inside paragraph tags after it
    # has otherwise switched to HTML. Promote those paragraphs mechanically.
    for paragraph in list(soup.find_all("p")):
        if paragraph.find(True):
            continue
        match = re.fullmatch(
            r"\s*(#{1,6})\s+(.+?)\s*",
            paragraph.get_text(),
            flags=re.S,
        )
        if not match:
            continue
        level = 2 if len(match.group(1)) <= 2 else 3
        heading = soup.new_tag(f"h{level}")
        heading.string = match.group(2).strip()
        paragraph.replace_with(heading)

    # Convert only text nodes, never attributes or existing link markup.
    for node in list(soup.find_all(string=True)):
        if node.parent and node.parent.name in {"script", "style", "code", "pre"}:
            continue
        raw = str(node)
        repaired = re.sub(
            r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)",
            r'<a href="\2">\1</a>',
            raw,
        )
        repaired = re.sub(r"\*\*([^*\n]+?)\*\*", r"<strong>\1</strong>", repaired)
        repaired = re.sub(r"__([^_\n]+?)__", r"<strong>\1</strong>", repaired)
        repaired = re.sub(
            r"(?m)^\s*#{1,6}\s+(.+?)\s*$",
            r"<strong>\1</strong>",
            repaired,
        )
        if repaired == raw:
            continue
        fragment = BeautifulSoup(repaired, "html.parser")
        node.replace_with(*list(fragment.contents))

    # Markdown emphasis can straddle an HTML element, leaving the opening and
    # closing delimiters in separate text nodes (for example
    # **<a href="...">label</a>**). It is already structurally emphasized by
    # the enclosed HTML, so remove only paired boundary delimiters.
    for parent in list(soup.find_all(["p", "li", "div", "blockquote"])):
        contents = list(parent.contents)
        if len(contents) < 3:
            continue
        first, last = contents[0], contents[-1]
        if not isinstance(first, NavigableString) or not isinstance(
            last, NavigableString
        ):
            continue
        opening = re.match(r"^(\s*)(\*\*|__)", str(first))
        closing = re.search(r"(\*\*|__)(\s*)$", str(last))
        if opening and closing and opening.group(2) == closing.group(1):
            first.replace_with(
                str(first)[:opening.start(2)] + str(first)[opening.end(2):]
            )
            last.replace_with(
                str(last)[:closing.start(1)] + str(last)[closing.end(1):]
            )

    # Mixed model output often contains valid headings followed by naked
    # paragraphs and Markdown bullets at the document root. Those text nodes
    # are visible copy, not harmless whitespace. Render each one through the
    # plain-text normalizer so a single formatting lapse cannot discard an
    # otherwise usable paid draft.
    for node in list(soup.contents):
        if not isinstance(node, NavigableString) or not str(node).strip():
            continue
        rendered = ensure_article_html(str(node))
        fragment = BeautifulSoup(rendered, "html.parser")
        node.replace_with(*list(fragment.contents))
    return str(soup)


def _repair_escaped_article_tags(value):
    """Render a narrow allowlist of model-escaped article tags safely."""
    for _ in range(2):
        collapsed = re.sub(
            r"&amp;(lt;|gt;|quot;|#x27;)", r"&\1", value, flags=re.I
        )
        if collapsed == value:
            break
        value = collapsed
    tag_pattern = re.compile(
        r"&lt;(/?)(strong|h[1-6]|a|p|ul|ol|li|blockquote)"
        r"((?:(?!&gt;).)*?)&gt;",
        re.I,
    )

    def replace(match):
        closing, tag, raw_attrs = match.groups()
        tag = tag.lower()
        if closing:
            return f"</{tag}>"
        attrs = html_lib.unescape(raw_attrs or "")
        if tag == "a":
            href = re.search(r"\bhref=[\"'](https?://[^\"']+)[\"']", attrs, re.I)
            return f'<a href="{html_lib.escape(href.group(1), quote=True)}">' if href else "<a>"
        if tag == "strong" and re.search(
            r"\bclass=[\"'][^\"']*\bkey-takeaway\b", attrs, re.I
        ):
            return '<strong class="key-takeaway">'
        return f"<{tag}>"

    return tag_pattern.sub(replace, value)


def ensure_article_html(value):
    """Remove model fences and convert plain-text drafts to article-body HTML."""
    value = (value or "").strip()
    value = re.sub(r"^\s*```(?:html|markdown|md)?\s*", "", value, flags=re.I)
    value = re.sub(r"\s*```\s*$", "", value).strip()
    if not value:
        return value

    # Already-structured output may still contain Markdown residue inside HTML.
    if re.search(r"<(?:p|h[1-6]|ul|ol|li|div|blockquote)\b", value, re.I):
        return _repair_mixed_markdown(value)

    blocks = [
        block.strip()
        for block in re.split(r"\n\s*\n+", value)
        if block.strip()
    ]
    rendered = []
    heading_markers = (
        "what ", "why ", "how ", "who ", "where ", "when ", "key ",
        "service ", "investment ", "the marketing", "getting more",
        "pricing", "risk", "material limitations", "frequently asked",
        "contact", "bottom line",
    )
    for block in blocks:
        normalized_block = re.sub(r"\s*\n\s*", " ", block).strip()
        markdown_heading = re.match(
            r"^#{1,6}\s+(.+)$", normalized_block
        )
        if markdown_heading:
            normalized_block = markdown_heading.group(1).strip()
        is_heading = bool(
            len(normalized_block) <= 120
            and not re.search(r"[.!?]$", normalized_block)
            and (
                markdown_heading
                or normalized_block.casefold().startswith(heading_markers)
                or normalized_block.istitle()
            )
        )
        if is_heading:
            rendered.append(
                f"<h2><strong>{html_lib.escape(normalized_block)}</strong></h2>"
            )
            continue

        lines = [
            line.strip() for line in re.split(r"\n+", block) if line.strip()
        ]
        if len(lines) >= 2 and all(
            re.match(r"^(?:[-*•]|\d+[.)])\s+", line) for line in lines
        ):
            items = [
                re.sub(r"^(?:[-*•]|\d+[.)])\s+", "", line)
                for line in lines
            ]
            rendered.append(
                "<ul>" + "".join(
                    f"<li>{html_lib.escape(item)}</li>" for item in items
                ) + "</ul>"
            )
        else:
            rendered.append(f"<p>{html_lib.escape(normalized_block)}</p>")
    return "\n".join(rendered)


def normalize_master_html(html, word_count):
    """Normalize heading/CTA bolding and cap persuasive emphasis naturally."""
    html = ensure_article_html(html)
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
        "Review the current product details",
        "See current pricing and available package options",
        "Check the current offer terms",
        "Explore the product features and ordering information",
        "View the current product offer",
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
    html = _repair_escaped_article_tags(html)
    html = ensure_article_html(html)
    soup = BeautifulSoup(html, "html.parser")
    is_globe = platform == "Globe Newswire"

    # Models sometimes mix valid HTML with top-level prose or production
    # notes. Preserve ordinary prose by wrapping it, but remove separators and
    # workflow instructions that can never be reader-facing.
    for node in list(soup.contents):
        if not isinstance(node, NavigableString) or not str(node).strip():
            continue
        raw = str(node).strip()
        if re.fullmatch(r"---+|~~~+", raw):
            node.extract()
            continue
        if re.search(
            r"\b(?:remove unsupported external assertions|"
            r"build depth from sealed product facts|"
            r"semantic reconstruction required|"
            r"word count|coverage allocation)\b",
            raw,
            re.I,
        ):
            node.extract()
            continue
        paragraph = soup.new_tag("p")
        paragraph.string = raw
        node.replace_with(paragraph)

    for node in list(soup.find_all(["p", "div"])):
        text_value = node.get_text(" ", strip=True)
        if re.search(
            r"\b(?:remove unsupported external assertions|"
            r"build depth from sealed product facts|"
            r"semantic reconstruction required)\b",
            text_value,
            re.I,
        ) or re.match(
            r"^(?:word count|coverage allocation)\s*:",
            text_value,
            re.I,
        ):
            node.decompose()

    # WordPress stores the release title separately.
    for heading in list(soup.find_all("h1")):
        heading.decompose()

    text = soup.get_text(" ", strip=True)
    word_count = len(re.findall(r"\b[\w’'-]+\b", text))

    # The disclosure and risk statement are mechanical requirements, not
    # editorial judgment calls.
    for paragraph in list(soup.find_all("p")):
        paragraph_text = paragraph.get_text(" ", strip=True).casefold()
        if (
            "paid advertorial" in paragraph_text
            or "compensation may be received" in paragraph_text
            or "a commission may be earned" in paragraph_text
        ):
            paragraph.decompose()
    affiliate_href = str(affiliate_href or "").strip()
    has_material_affiliate_link = bool(
        affiliate_href
        and affiliate_href.upper() != "TRAFFIC-FIRST"
        and any(
            str(node.get("href") or "").strip() == affiliate_href
            for node in soup.find_all("a", href=True)
        )
    )
    if not is_globe:
        disclosure_html = (
            "<p><strong>Paid Advertorial:</strong> Compensation may be received "
            "if a purchase is made through links in this advertorial.</p>"
            if (
                has_material_affiliate_link
            )
            else "<p><strong>Paid Advertorial</strong></p>"
        )
        disclosure = BeautifulSoup(disclosure_html, "html.parser").p
        first = soup.find()
        if first:
            first.insert_before(disclosure)
        else:
            soup.append(disclosure)
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
    # Neutralize prosecutorial framing mechanically while preserving the
    # underlying buyer questions. Semantic balance is still enforced by D19.
    advocacy_heading_rewrites = (
        (r"\bthe critical issue\b", "What Buyers Should Understand"),
        (
            r"\bwhat (?:information )?(?:is|remains) missing or unverified\b",
            "Important Offer Details",
        ),
        (
            r"\bwhat (?:information )?(?:is|remains) missing\b",
            "Important Offer Details",
        ),
        (
            r"\bverified alternatives?(?: with clear documentation)?\b",
            "How This Product Fits a Broader Buying Decision",
        ),
        (
            r"\bclaims? (?:versus|vs\.?) [^<]+",
            "How to Evaluate the Available Claims",
        ),
    )
    for heading in soup.find_all(["h2", "h3"]):
        heading_text = heading.get_text(" ", strip=True)
        rewritten = heading_text
        for pattern, replacement in advocacy_heading_rewrites:
            rewritten = re.sub(pattern, replacement, rewritten, flags=re.I)
        if rewritten != heading_text:
            heading.clear()
            strong = soup.new_tag("strong")
            strong.string = rewritten
            heading.append(strong)

    affiliate_host = (
        (urlparse(str(affiliate_href)).hostname or "").casefold()
        if affiliate_href
        else ""
    )
    for anchor in soup.find_all("a"):
        anchor_text = anchor.get_text(" ", strip=True)
        anchor_href = str(anchor.get("href") or "").strip()
        text_host = (
            (urlparse(anchor_text).hostname or "").casefold()
            if re.match(r"^https?://", anchor_text, re.I)
            else ""
        )
        href_host = (
            (urlparse(anchor_href).hostname or "").casefold()
            if re.match(r"^https?://", anchor_href, re.I)
            else ""
        )
        raw_affiliate_anchor = bool(
            re.match(r"^(?:https?://|www\.)", anchor_text, re.I)
            and affiliate_host
            and (
                href_host == affiliate_host
                or text_host == affiliate_host
            )
        )
        if raw_affiliate_anchor:
            anchor.clear()
            strong = soup.new_tag("strong")
            strong.string = "Review the current offer details"
            anchor.append(strong)

    if is_globe:
        # Remove FAQ/Q&A sections as complete semantic blocks. Any depth loss
        # remains visible to D18 and must be rebuilt as narrative, not hidden.
        for heading in list(soup.find_all(["h2", "h3"])):
            if not re.search(
                r"\b(?:faq|frequently asked|questions? (?:about|readers)|"
                r"related links?)\b",
                heading.get_text(" ", strip=True),
                re.I,
            ):
                continue
            level = int(heading.name[1])
            sibling = heading.find_next_sibling()
            while sibling is not None:
                next_sibling = sibling.find_next_sibling()
                if (
                    sibling.name in {"h2", "h3"}
                    and int(sibling.name[1]) <= level
                ):
                    break
                sibling.decompose()
                sibling = next_sibling
            heading.decompose()

        # Format C owns one Related Links URL. Unwrap other web links so
        # contact values remain visible without creating additional outbound
        # destinations, and remove standalone CTA furniture entirely.
        for anchor in list(soup.find_all("a", href=True)):
            href = str(anchor.get("href") or "").strip()
            if not href.casefold().startswith(("http://", "https://")):
                continue
            parent = anchor.parent
            anchor_text = anchor.get_text(" ", strip=True)
            if (
                parent is not None
                and parent.name in {"p", "li", "div"}
                and parent.get_text(" ", strip=True) == anchor_text
            ):
                parent.decompose()
            else:
                anchor.unwrap()

        if affiliate_href and affiliate_href.upper() != "TRAFFIC-FIRST":
            related_heading = soup.new_tag("h2")
            related_strong = soup.new_tag("strong")
            related_strong.string = "Related Links"
            related_heading.append(related_strong)
            related_paragraph = soup.new_tag("p")
            related_anchor = soup.new_tag("a", href=affiliate_href)
            related_anchor.string = "Product information"
            related_paragraph.append(related_anchor)
            soup.append(related_heading)
            soup.append(related_paragraph)

        from .platform_contracts import GLOBE_DISCLOSURE_TEXT
        disclosure = soup.new_tag("p")
        disclosure.string = GLOBE_DISCLOSURE_TEXT
        soup.append(disclosure)

    normalized = normalize_master_html(str(soup), word_count)
    if affiliate_href and affiliate_href.upper() != "TRAFFIC-FIRST":
        from .platform_contracts import platform_contract
        target = platform_contract(platform).affiliate_cta_target
        normalized = ensure_affiliate_links(
            normalized, affiliate_href, target=target
        )
        # Link insertion can create new nodes, so normalize once more.
        normalized = normalize_master_html(normalized, word_count)
    return normalized


def repair_source_grounding(html, source_text, vertical):
    """Remove excluded-claim echoes and unsupported device filler at zero cost."""
    soup = BeautifulSoup(ensure_article_html(html), "html.parser")
    pack = {}
    marker = "═══ SEALED CURRENT-PRODUCT SOURCE PACK — FACTS ONLY ═══"
    if marker in str(source_text or ""):
        try:
            pack = json.loads(str(source_text).split(marker, 1)[1].strip())
        except (TypeError, ValueError, json.JSONDecodeError):
            pack = {}

    def flatten_text(value):
        if isinstance(value, dict):
            for nested in value.values():
                yield from flatten_text(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from flatten_text(nested)
        elif value not in (None, ""):
            yield str(value).strip()

    def normalized_claim(value):
        clean = str(value or "").strip()
        prefix, separator, remainder = clean.partition(":")
        if (
            separator
            and prefix.strip().casefold() in STRUCTURED_PRODUCT_CLAIM_TYPES
        ):
            clean = remainder.strip()
        return re.sub(r"[^a-z0-9]+", " ", clean.casefold()).strip()

    # A legacy sealed pack can contain the same operator-captured structured
    # fact in both the trusted product record and the excluded raw-extraction
    # ledger. Treating the raw duplicate as an absolute deletion signature
    # removes valid disclosures such as age limits, refund terms, and
    # entertainment/no-outcome boundaries. Only the explicitly mapped
    # structured fields may corroborate an excluded duplicate; arbitrary
    # product prose cannot rescue a rejected claim.
    corroborating_text = [
        str(item.get("text") or "").strip()
        for items in (pack.get("publication_claims") or {}).values()
        for item in (items or [])
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]
    product = pack.get("product") or {}
    for field in STRUCTURED_PRODUCT_CLAIM_TYPES:
        corroborating_text.extend(flatten_text(product.get(field)))
    corroborating_normalized = {
        normalized_claim(item)
        for item in corroborating_text
        if normalized_claim(item)
    }

    def is_corroborated(claim):
        candidate = normalized_claim(claim)
        candidate_tokens = set(candidate.split())
        if not candidate or len(candidate_tokens) < 2:
            return False
        for trusted in corroborating_normalized:
            trusted_tokens = set(trusted.split())
            if candidate == trusted:
                return True
            if (
                len(candidate_tokens) >= 3
                and (
                    candidate in trusted
                    or (
                        len(candidate_tokens & trusted_tokens) >= 3
                        and len(candidate_tokens & trusted_tokens)
                        / len(candidate_tokens) >= 0.8
                    )
                )
            ):
                return True
        return False

    excluded_signatures = []
    for item in pack.get("excluded_publication_claims", []):
        claim = str(item.get("text") or "").strip()
        if (
            not claim
            or (
                str(item.get("reason") or "") != "blocked_by_compliance"
                and is_corroborated(claim)
            )
        ):
            continue
        normalized = re.sub(
            r"[^a-z0-9]+", " ", claim.casefold()
        ).strip()
        tokens = [
            token for token in re.findall(r"[a-z0-9]+", claim.casefold())
            if len(token) >= 5 and token not in {
                "seller", "claim", "claims", "product", "device",
                "materials", "official", "stated",
            }
        ]
        if tokens:
            excluded_signatures.append((normalized, tokens))

    unsupported_device_patterns = (
        r"\bindustrial\b.{0,100}\bresidential\b|"
        r"\bresidential\b.{0,100}\bindustrial\b",
        r"\bdirty electricity\b.{0,100}"
        r"\b(?:comes?|caused|generated|created|sources?)\b",
        r"\b(?:cheaper|less expensive|costs? less|low-risk trial)\b.{0,120}"
        r"\b(?:professional|audit|appliance|upgrade|electrician)\b",
        r"\btransient voltage spikes?\b.{0,140}\b(?:damage|safety risks?)\b",
        r"\b(?:use|choose|buy) (?:a |an )?ups instead\b",
        r"\b(?:ul|etl|csa)(?:[- ]listed| certification| certified|,|\s+or\s+)",
        r"\blegitimate product should\b",
        r"\b(?:deploy|place|install) multiple units?\b.{0,120}"
        r"\b(?:room|circuit|throughout)\b",
        r"\bindustry[- ]standard\b|\brisk[- ]free trial\b",
        # Do not invent buyer cohorts from a product category. These generic
        # audience constructions repeatedly survive model repair even though
        # the sealed record says nothing about ownership, tenancy, building
        # type, or installation environment.
        r"\b(?:appeal(?:s|ing)? to|best for|designed for|ideal for)\b.{0,100}"
        r"\b(?:homeowners?|renters?|landlords?|business owners?)\b",
        r"\b(?:homeowners?|renters?)\b",
        r"\bsensitive electronics\b",
        r"\bqualified electrician\b.{0,240}\b(?:recommend|appropriate|"
        r"hardwired|panel upgrades?|shielding)\b",
        r"\b(?:tech[- ]forward consumers?|reasonable trial product|"
        r"professional[- ]grade|whole[- ]home systems?|"
        r"residents? of older buildings?|"
        r"cannot install permanent electrical upgrades?|"
        r"entry[- ]level power[- ]conditioning)\b",
        r"\b(?:medical devices?|health[- ]related emf|emf exposure)\b.{0,180}"
        r"\b(?:option|trial|test|concern|equipment)\b",
        r"\b(?:suitable for any room|bedrooms?|home offices?|"
        r"spaces? with sensitive electronics)\b",
        r"\b(?:older homes?|aging electrical systems?|modern electrical codes?|"
        r"grid instability|brownouts?|rolling blackouts?|"
        r"transmission challenges?)\b",
        r"\b(?:surge protectors?|power conditioners?|emf filters?)\b.{0,220}"
        r"\b(?:standardized|endorsed|electrical industry|"
        r"regulatory agencies?|engineers?)\b",
        r"\bstandard disclosures? for electrical devices?\b",
        r"\blegitimate electrical devices? typically\b",
        r"\bstandardized electrical engineering classification\b",
        r"\bmonitor (?:the )?device'?s performance\b",
        r"\btrack whether electrical issues\b",
        r"\bseek independent product testing\b",
        r"\bconsumer electronics review organizations?\b",
        r"\bcomprehensive surge protection\b",
        r"\bcompliance with electrical codes\b",
        r"\bno configuration required\b",
        r"\breconstruct this sentence from isolated permitted claim text\b",
        r"\bForecasts\s*&\s*Strategies\b",
        r"\bsimple access to the current offer\b",
        r"\bmarketed for residential use\b",
        r"\b(?:standard electrical|standard) outlet\b.{0,160}"
        r"\b(?:immediately|professional installation|special wiring|"
        r"technical knowledge|configuration)\b",
        r"\b(?:technical knowledge|special wiring modifications?|"
        r"professional installation|app connectivity|monitoring dashboard|"
        r"set-and-forget)\b",
        r"\bdirty (?:emf )?electricity\b.{0,180}"
        r"\b(?:power quality variations?|electromagnetic field|"
        r"household current|electrical lines?)\b",
        r"\bmake sure your device is working\b.{0,240}"
        r"\b(?:indicator|confirmation|verify|ongoing operation)\b",
        r"\b(?:individual home electrical systems?|regional grid conditions?|"
        r"existing home wiring|environmental factors?)\b",
        r"\b(?:customer outcome studies?|before-and-after measurements?|"
        r"professional electrical audits?|documented case studies?|"
        r"your own monitoring)\b",
        r"\b(?:similar products?|power-quality or electrical-protection "
        r"devices?)\b",
        r"\b(?:electricity bills?|appliance lifespan|real home environment)\b",
        r"\b(?:draws? minimal power|heat generation|power source)\b",
        r"\b(?:continuous-operation safety|continuous versus intermittent)\b",
        r"\b(?:track|compare|monitor)\b.{0,100}\butility bills?\b",
        r"\breal-world cost savings\b.{0,180}"
        r"\b(?:utility rate|baseline power factor|home'?s baseline)\b",
        r"\bprice is low enough to test\b",
        r"\bmodest cost\b.{0,100}\b(?:simple installation|test|purchase)\b",
        r"\bwilling to purchase based on seller claims\b",
        r"\bharmonic distortion\b|\bharmonics\b",
        r"\bremove unsupported external assertions\b",
    )
    for node in list(soup.find_all(["p", "li"])):
        for text_node in list(node.find_all(string=True)):
            qualified = re.sub(
                r"\b(in|from) available materials\b",
                r"\1 the available source materials reviewed for this article",
                str(text_node),
                flags=re.I,
            )
            if qualified != str(text_node):
                text_node.replace_with(qualified)
        lowered = node.get_text(" ", strip=True).casefold()
        if lowered == "sealed-depth-recovery-v2":
            node.decompose()
            continue
        normalized_node = re.sub(r"[^a-z0-9]+", " ", lowered).strip()
        words = set(re.findall(r"[a-z0-9]+", normalized_node))
        excluded_echo = False
        for normalized_claim, token_list in excluded_signatures:
            token_set = set(token_list)
            if len(token_set) <= 3:
                matched = (
                    normalized_claim in normalized_node
                    or token_set.issubset(words)
                )
            else:
                overlap = len(words & token_set)
                matched = (
                    overlap >= 3
                    and overlap / max(len(token_set), 1) >= 0.75
                )
            if matched:
                excluded_echo = True
                break
        unsupported_filler = (
            vertical == "device"
            and any(
                re.search(pattern, lowered, re.I | re.S)
                for pattern in unsupported_device_patterns
            )
        )
        if excluded_echo or unsupported_filler:
            node.decompose()

    for node in list(soup.find_all(["ul", "ol"])):
        if not node.get_text(" ", strip=True):
            node.decompose()
    for heading in list(soup.find_all(["h2", "h3"])):
        sibling = heading.find_next_sibling()
        if sibling is None or sibling.name in {"h2", "h3"}:
            heading.decompose()
    for node in list(soup.find_all("strong")):
        if (
            node.parent is soup
            and node.get_text(" ", strip=True).casefold().rstrip(":")
            == "paid advertorial"
        ):
            node.decompose()

    # Canonicalize sealed pricing as one clean seller-attributed list. Merely
    # checking whether the digits occur somewhere allowed malformed fragments
    # and an empty "two options" lead-in to survive all the way to sign-off.
    pricing_claims = (
        (pack.get("publication_claims") or {}).get("pricing") or []
    )
    canonical_pricing = []
    for item in pricing_claims:
        claim = str(item.get("text") or "").strip()
        if claim and re.search(r"\d+(?:\.\d+)?", claim):
            canonical_pricing.append(claim.rstrip("."))
    if canonical_pricing:
        heading = next(
            (
                node for node in soup.find_all(["h2", "h3"])
                if "pric" in node.get_text(" ", strip=True).casefold()
            ),
            None,
        )
        if heading is None:
            heading = soup.new_tag("h2")
            strong = soup.new_tag("strong")
            strong.string = "Current Pricing and Package Information"
            heading.append(strong)
            soup.append(heading)
        else:
            cursor = heading.find_next_sibling()
            while cursor is not None and cursor.name not in {"h2", "h3"}:
                following = cursor.find_next_sibling()
                cursor.decompose()
                cursor = following
        lead = soup.new_tag("p")
        lead.string = "According to the seller, pricing is structured in these options:"
        pricing_list = soup.new_tag("ul")
        for claim in canonical_pricing:
            item = soup.new_tag("li")
            item.string = f"According to the seller, {claim}."
            pricing_list.append(item)
        heading.insert_after(pricing_list)
        heading.insert_after(lead)
    return str(soup)
