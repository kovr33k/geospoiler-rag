# Domain Docs

## Layout

GeoSpoiler RAG is a single-context project. Its domain glossary, when present, is `CONTEXT.md` at the Git root. Architectural decision records, when present, live in `docs/adr/`.

## Before exploring

- Read `CONTEXT.md` when it exists, and use its established vocabulary.
- Read decision records relevant to the area being changed.
- If these files do not exist, proceed without creating placeholders or treating their absence as a blocker. Create domain records lazily through the domain-modeling workflow when terms or decisions are actually resolved.
- For the existing architecture and data contracts, consult `GeoSpoiler-RAG-Hybrid/ARCHITECTURE.md` and `GeoSpoiler-RAG-Hybrid/DATA_CONTRACTS.md`.
- For this ingestion work, consult the specification and decision record linked from the root instruction file.

## Writing

Use existing terms such as normalized material, Enriched v2 card, source provenance, YouTube generation, local index and explicit LightRAG load. Keep glossary definitions separate from implementation plans.

If a proposed change conflicts with an architectural decision, identify the conflict before changing that behavior.

Domain documentation and tracker setup apply only to GeoSpoiler RAG.
