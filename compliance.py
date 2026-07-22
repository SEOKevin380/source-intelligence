"""
Source Intelligence — Rule-Based Compliance Engine
====================================================
Replaces flat word-substitution compliance with structured rule evaluation.

Each rule is typed by:
- Offering types it applies to (or all)
- Jurisdictions (US, EU, etc.)
- Channels (wordpress, globe, accesswire, etc.)
- Severity (block, review, warning)

Evaluation produces a ComplianceState that determines what can proceed
and what requires human intervention.
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Set

from entities import OfferingType


class ComplianceState(Enum):
    """Possible compliance outcomes, ordered by severity."""
    BLOCKED = "blocked"                          # Cannot proceed under any circumstances
    HUMAN_REVIEW_REQUIRED = "human_review"       # Must be reviewed by a human
    READY_FOR_EDITORIAL_REVIEW = "editorial"     # Automated checks passed, needs editor
    CLEARED = "cleared"                          # All checks passed


class Severity(Enum):
    """Rule severity levels."""
    BLOCK = "block"        # Hard stop — cannot publish
    REVIEW = "review"      # Requires human review
    WARNING = "warning"    # Flag but don't block


@dataclass
class ComplianceResult:
    """Result of evaluating a single compliance rule against content."""
    rule_id: str
    state: ComplianceState
    matched_text: str = ""
    safe_alternative: str = ""
    scope: str = ""                    # What part of content matched
    description: str = ""


@dataclass
class ComplianceReport:
    """Aggregate compliance report for a piece of content."""
    overall_state: ComplianceState = ComplianceState.CLEARED
    results: List[ComplianceResult] = field(default_factory=list)
    blocks: int = 0
    reviews: int = 0
    warnings: int = 0

    def add(self, result: ComplianceResult):
        """Add a result and update overall state."""
        self.results.append(result)
        if result.state == ComplianceState.BLOCKED:
            self.blocks += 1
            self.overall_state = ComplianceState.BLOCKED
        elif result.state == ComplianceState.HUMAN_REVIEW_REQUIRED:
            self.reviews += 1
            if self.overall_state not in (ComplianceState.BLOCKED,):
                self.overall_state = ComplianceState.HUMAN_REVIEW_REQUIRED
        elif result.state == ComplianceState.READY_FOR_EDITORIAL_REVIEW:
            self.warnings += 1
            if self.overall_state == ComplianceState.CLEARED:
                self.overall_state = ComplianceState.READY_FOR_EDITORIAL_REVIEW

    def summary(self) -> str:
        """Human-readable summary."""
        return (
            f"{self.overall_state.value}: "
            f"{self.blocks} blocks, {self.reviews} reviews, {self.warnings} warnings"
        )


@dataclass
class ComplianceRule:
    """A single compliance rule definition."""
    rule_id: str
    description: str
    severity: Severity
    offering_types: Optional[Set[OfferingType]] = None  # None = all types
    channels: Optional[Set[str]] = None                  # None = all channels
    jurisdictions: Optional[Set[str]] = None             # None = all jurisdictions
    patterns: List[re.Pattern] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    safe_alternative: str = ""

    def applies_to(self, offering_type: OfferingType,
                   channel: str = "", jurisdiction: str = "US") -> bool:
        """Check if this rule applies to the given context."""
        if self.offering_types and offering_type not in self.offering_types:
            return False
        if self.channels and channel and channel not in self.channels:
            return False
        if self.jurisdictions and jurisdiction not in self.jurisdictions:
            return False
        return True

    def check(self, text: str) -> Optional[ComplianceResult]:
        """Check text against this rule. Returns result if violated, None if clean."""
        text_lower = text.lower()

        # Check keyword matches
        for kw in self.keywords:
            if kw.lower() in text_lower:
                state = _severity_to_state(self.severity)
                return ComplianceResult(
                    rule_id=self.rule_id,
                    state=state,
                    matched_text=kw,
                    safe_alternative=self.safe_alternative,
                    description=self.description,
                )

        # Check regex patterns
        for pat in self.patterns:
            match = pat.search(text)
            if match:
                state = _severity_to_state(self.severity)
                return ComplianceResult(
                    rule_id=self.rule_id,
                    state=state,
                    matched_text=match.group(0),
                    safe_alternative=self.safe_alternative,
                    description=self.description,
                )

        return None


def _severity_to_state(severity: Severity) -> ComplianceState:
    """Map severity to compliance state."""
    if severity == Severity.BLOCK:
        return ComplianceState.BLOCKED
    elif severity == Severity.REVIEW:
        return ComplianceState.HUMAN_REVIEW_REQUIRED
    return ComplianceState.READY_FOR_EDITORIAL_REVIEW


# ============================================================================
# BUILT-IN RULES (loaded from config.py data)
# ============================================================================

def _build_builtin_rules() -> List[ComplianceRule]:
    """Build compliance rules from config.py constants."""
    from config import (
        CLAIM_RED_FLAGS, HEDGE_ALTERNATIVES,
        CVD9_DISEASE_TERMS, CVD9_REVERSAL_VERBS,
        DECEPTIVE_CLAIM_PATTERNS, CVD9_STANDING_DECLINES,
        ACCESSWIRE_BLOCKLIST, GLOBE_BLOCKLIST,
    )

    rules = []

    # ── CVD-9: Disease reversal claims (BLOCK) ──
    # Build patterns: reversal_verb + disease_term
    cvd9_patterns = []
    for verb in CVD9_REVERSAL_VERBS:
        for disease in CVD9_DISEASE_TERMS:
            pat = re.compile(
                rf"\b{re.escape(verb)}\b.{{0,40}}\b{re.escape(disease)}\b",
                re.IGNORECASE
            )
            cvd9_patterns.append(pat)
    rules.append(ComplianceRule(
        rule_id="CVD9_DISEASE_REVERSAL",
        description="Disease-reversal claim: cannot be attributed, hedged, or softened",
        severity=Severity.BLOCK,
        patterns=cvd9_patterns,
    ))

    # ── Deceptive claim patterns (BLOCK) ──
    rules.append(ComplianceRule(
        rule_id="DECEPTIVE_CLAIMS",
        description="Physically impossible or deceptive claim pattern",
        severity=Severity.BLOCK,
        patterns=list(DECEPTIVE_CLAIM_PATTERNS),
    ))

    # ── Standing declines (BLOCK) ──
    for decline_id, decline in CVD9_STANDING_DECLINES.items():
        rules.append(ComplianceRule(
            rule_id=f"STANDING_DECLINE_{decline_id.upper()}",
            description=decline["description"],
            severity=Severity.BLOCK,
            keywords=decline["keywords"],
        ))

    # ── Claim red flags (REVIEW) ──
    for flag in CLAIM_RED_FLAGS:
        alt = HEDGE_ALTERNATIVES.get(flag, "")
        rules.append(ComplianceRule(
            rule_id=f"RED_FLAG_{flag.upper().replace(' ', '_')}",
            description=f"Unhedged health claim: '{flag}'",
            severity=Severity.REVIEW,
            keywords=[flag],
            safe_alternative=alt,
            offering_types={
                OfferingType.SUPPLEMENT, OfferingType.TOPICAL,
                OfferingType.FOOD, OfferingType.CANNABIS,
                OfferingType.RESEARCH_PEPTIDE,
            },
        ))

    # ── AccessWire R12 blocklist (BLOCK on AccessWire channel) ──
    rules.append(ComplianceRule(
        rule_id="ACCESSWIRE_R12",
        description="AccessWire R12 restricted term",
        severity=Severity.BLOCK,
        channels={"accesswire"},
        keywords=ACCESSWIRE_BLOCKLIST,
    ))

    # ── Globe Newswire blocklist (BLOCK on Globe channel) ──
    globe_keywords = []
    for category_terms in GLOBE_BLOCKLIST.values():
        globe_keywords.extend(category_terms)
    rules.append(ComplianceRule(
        rule_id="GLOBE_BLOCKLIST",
        description="Globe Newswire restricted phrase",
        severity=Severity.BLOCK,
        channels={"globe"},
        keywords=globe_keywords,
    ))

    # ── FDA disclaimer requirement (WARNING for ingestibles) ──
    rules.append(ComplianceRule(
        rule_id="FDA_DISCLAIMER_REQUIRED",
        description="FDA disclaimer required for ingestible products",
        severity=Severity.WARNING,
        offering_types={
            OfferingType.SUPPLEMENT, OfferingType.FOOD,
        },
        # This rule is checked structurally, not via keyword match
        keywords=[],
    ))

    return rules


class ComplianceEngine:
    """Evaluates content against compliance rules.

    Rules are filtered by offering_type, channel, and jurisdiction
    before evaluation. Only applicable rules are checked.
    """

    def __init__(self):
        self._rules: List[ComplianceRule] = []
        self._load_builtin_rules()

    def _load_builtin_rules(self):
        """Load built-in rules from config.py data."""
        self._rules = _build_builtin_rules()

    def add_rule(self, rule: ComplianceRule):
        """Add a custom compliance rule."""
        self._rules.append(rule)

    def evaluate(self, text: str,
                 offering_type: OfferingType = OfferingType.SUPPLEMENT,
                 channel: str = "wordpress",
                 jurisdiction: str = "US") -> ComplianceReport:
        """Evaluate text against all applicable compliance rules.

        Returns a ComplianceReport with the overall state and individual results.
        """
        report = ComplianceReport()

        for rule in self._rules:
            if not rule.applies_to(offering_type, channel, jurisdiction):
                continue

            # Skip structural rules that have no keywords/patterns
            if not rule.keywords and not rule.patterns:
                continue

            result = rule.check(text)
            if result:
                report.add(result)

        return report

    def check_fda_disclaimer(self, text: str,
                              offering_type: OfferingType) -> Optional[ComplianceResult]:
        """Check if FDA disclaimer is present when required.

        Returns a ComplianceResult if disclaimer is missing, None if present or not required.
        """
        ingestible_types = {
            OfferingType.SUPPLEMENT, OfferingType.FOOD,
        }
        if offering_type not in ingestible_types:
            return None

        fda_indicators = [
            "not been evaluated by the food and drug administration",
            "not intended to diagnose, treat, cure, or prevent",
            "fda disclaimer",
            "these statements have not been evaluated",
        ]
        text_lower = text.lower()
        for indicator in fda_indicators:
            if indicator in text_lower:
                return None  # Disclaimer found

        return ComplianceResult(
            rule_id="FDA_DISCLAIMER_MISSING",
            state=ComplianceState.READY_FOR_EDITORIAL_REVIEW,
            description="FDA disclaimer is required but not found in content",
        )

    def get_rules_for_context(self, offering_type: OfferingType,
                               channel: str = "",
                               jurisdiction: str = "US") -> List[ComplianceRule]:
        """Get all rules applicable to a given context."""
        return [
            r for r in self._rules
            if r.applies_to(offering_type, channel, jurisdiction)
        ]

    @property
    def rule_count(self) -> int:
        """Total number of loaded rules."""
        return len(self._rules)
