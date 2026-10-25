# GeoSpoiler RAG 🌐

GeoSpoiler is an advanced, open-source **Multi-modal Hybrid RAG (Retrieval-Augmented Generation)** architecture designed for real-time OSINT (Open Source Intelligence), fact-checking, and social media analysis.

By combining **Knowledge Graphs (LightRAG)** with **Semantic Search (FTS & Enriched Memory Cards)**, GeoSpoiler ingests raw, unstructured intelligence from various media streams and builds a highly accurate, queryable knowledge base with strict source-grounding guardrails.

## ✨ Core Capabilities

- **Multi-Modal Ingestion**: Natively fetches and normalizes text, images (via Vision APIs), voice messages/video (via Whisper transcription), and documents.
- **Cross-Platform Support**: Seamlessly processes data from **Telegram channels**, **YouTube** (videos/shorts), **Instagram**, and standard web articles.
- **Hybrid Search Architecture**: 
  - **LightRAG Graph**: Extracts entities and relationships to build a comprehensive knowledge graph.
  - **Enriched Memory Layer**: Uses an LLM to extract high-level analytical claims, theses, and visual B-roll notes before indexing.
  - **Multi-index Retrieval Composer**: Supports specialized search modes like `recall` (broad), `thesis` (analytical claims), `entity` (strict actor search), and `broll` (visual footage search).
- **Enterprise-Grade Evaluation**: Built with strict "Golden Set" benchmarks, source-selection validation, and continuous LLM verification probes to prevent hallucinations and ensure rigorous source grounding.

## 🏗️ Architecture

The pipeline follows a robust 4-stage process:
1. **Fetch**: Connects to platforms (e.g., Telegram) to download messages, images, and videos.
2. **Normalize**: Converts diverse media into standardized `.txt` representations (including Whisper audio transcription and Vision model image descriptions).
3. **Enrich**: Analyzes the raw text to extract structured intelligence (summaries, key facts, entities, quotes, theses).
4. **Load & Serve**: Compiles the data into a Graph Database (LightRAG) and local SQLite instances for lightning-fast hybrid querying.

## 🚀 Getting Started

The main application code, setup instructions, and CLI documentation are located in the `GeoSpoiler-RAG-Hybrid` directory.

👉 **[See the full technical documentation and installation guide here](GeoSpoiler-RAG-Hybrid/README.md)**

---

*Built for the open-source community to empower fact-checkers, journalists, and developers with robust, hallucination-resistant LLM workflows.*
