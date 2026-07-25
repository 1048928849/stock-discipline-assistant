from __future__ import annotations

from app.analysis.contracts import ConceptChainContext, ConceptChainInput


_RANK = {
    "CORE_BUSINESS": 5,
    "IMPORTANT_BUSINESS": 4,
    "MARGINAL_BENEFIT": 3,
    "CONCEPT_ASSOCIATION": 2,
    "INSUFFICIENT_EVIDENCE": 1,
    "REJECTED": 0,
}


def analyze_concept_chain(value: ConceptChainInput) -> ConceptChainContext:
    grouped = {}
    for item in value.concepts:
        grouped.setdefault(item.concept, []).append(item)
    conflicts = []
    candidates = []
    evidence_refs = set()
    for name, mappings in sorted(grouped.items()):
        statuses = {item.relevance for item in mappings}
        evidence_refs.update(ref for item in mappings for ref in item.evidence_refs)
        if "REJECTED" in statuses and len(statuses) > 1:
            conflicts.append(f"{name}: conflicting relevance evidence")
            candidates.append((name, "INSUFFICIENT_EVIDENCE"))
        else:
            candidates.append((name, max(statuses, key=lambda item: _RANK[item])))
    candidates.sort(key=lambda item: (-_RANK[item[1]], item[0]))
    core_concept, relevance = (
        candidates[0] if candidates else (None, "INSUFFICIENT_EVIDENCE")
    )
    chain = None
    if value.chain_positions:
        chain = sorted(
            value.chain_positions,
            key=lambda item: (-_RANK[item.relevance], item.chain, item.node),
        )[0]
        evidence_refs.update(chain.evidence_refs)
    mainlines = set(value.current_mainlines)
    is_mainline = core_concept in mainlines if core_concept else False
    is_core = is_mainline and relevance in {"CORE_BUSINESS", "IMPORTANT_BUSINESS"}
    if conflicts:
        relevance = "INSUFFICIENT_EVIDENCE"
        is_core = False
    return ConceptChainContext(
        core_concept=core_concept,
        relevance=relevance,
        chain=chain.chain if chain else None,
        chain_node=chain.node if chain else None,
        primary_products=chain.primary_products if chain else (),
        revenue_relevance=chain.revenue_relevance if chain else "unknown",
        core_level=chain.core_level if chain else None,
        substitutability=chain.substitutability if chain else None,
        competitive_position=chain.competitive_position if chain else None,
        evidence_refs=tuple(sorted(evidence_refs)),
        conflicts=tuple(conflicts),
        is_current_mainline=is_mainline,
        is_mainline_core_company=is_core,
    )


__all__ = ["analyze_concept_chain"]
