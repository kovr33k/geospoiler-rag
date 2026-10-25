import json
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

from geospoiler import config

_GRAPHML_NS = {"g": "http://graphml.graphdrawing.org/xmlns"}
_URL_ENTITY_RE = re.compile(r"^(?:https?://|www\.)", re.IGNORECASE)
_DATE_ENTITY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2})?$")


def _load_graphml() -> tuple[list[dict], list[dict]]:
    path = config.RAG_STORAGE_DIR / "graph_chunk_entity_relation.graphml"
    if not path.exists():
        return [], []

    root = ET.parse(path).getroot()
    nodes = []
    edges = []

    for node in root.findall(".//g:node", _GRAPHML_NS):
        payload = {d.get("key"): (d.text or "").strip() for d in node.findall("g:data", _GRAPHML_NS)}
        payload["id"] = node.get("id", "")
        nodes.append(payload)

    for edge in root.findall(".//g:edge", _GRAPHML_NS):
        payload = {d.get("key"): (d.text or "").strip() for d in edge.findall("g:data", _GRAPHML_NS)}
        payload["source"] = edge.get("source", "")
        payload["target"] = edge.get("target", "")
        edges.append(payload)

    return nodes, edges


def _load_entity_docs() -> dict:
    path = config.RAG_STORAGE_DIR / "kv_store_full_entities.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _is_noise_entity(name: str) -> bool:
    if not name.strip():
        return True
    if _URL_ENTITY_RE.match(name):
        return True
    if _DATE_ENTITY_RE.match(name):
        return True
    if "t.me/" in name.lower():
        return True
    return False


def _collect_alias_groups(entity_names: list[str]) -> list[list[str]]:
    groups = defaultdict(set)
    for name in entity_names:
        groups[name.casefold()].add(name)
    return [
        sorted(values)
        for values in groups.values()
        if len(values) > 1
    ]


def _connected_components(edges: list[dict], node_ids: list[str]) -> tuple[int, int, int]:
    adj = defaultdict(set)
    for node_id in node_ids:
        adj[node_id]
    for edge in edges:
        adj[edge["source"]].add(edge["target"])
        adj[edge["target"]].add(edge["source"])

    isolated = sum(1 for neighbors in adj.values() if not neighbors)

    seen = set()
    component_sizes = []
    for node_id in adj:
        if node_id in seen:
            continue
        stack = [node_id]
        seen.add(node_id)
        size = 0
        while stack:
            current = stack.pop()
            size += 1
            for neighbor in adj[current]:
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)
        component_sizes.append(size)

    return isolated, len(component_sizes), max(component_sizes, default=0)


def build_quality_report() -> str:
    nodes, edges = _load_graphml()
    entity_docs = _load_entity_docs()

    node_ids = [node["id"] for node in nodes]
    type_counts = Counter(node.get("d1", "missing") for node in nodes)
    all_entity_names = []
    for doc in entity_docs.values():
        all_entity_names.extend(doc.get("entity_names", []))

    noise_entities = sorted({name for name in all_entity_names if _is_noise_entity(name)})
    alias_groups = _collect_alias_groups(all_entity_names)
    isolated_nodes, component_count, largest_component = _connected_components(edges, node_ids)

    allowed_types = {entity_type.casefold() for entity_type in config.LIGHTRAG_ENTITY_TYPES}
    non_whitelist_types = sorted(
        entity_type
        for entity_type in type_counts
        if entity_type and entity_type.casefold() not in allowed_types
    )

    by_degree = Counter()
    for edge in edges:
        by_degree[edge["source"]] += 1
        by_degree[edge["target"]] += 1

    lines = [
        "Graph quality report",
        "=" * 60,
        f"Nodes: {len(nodes)}",
        f"Edges: {len(edges)}",
        f"Isolated nodes: {isolated_nodes}",
        f"Connected components: {component_count}",
        f"Largest component: {largest_component}",
        "",
        "Entity types:",
    ]
    for entity_type, count in type_counts.most_common():
        lines.append(f"  {count:>3}  {entity_type}")

    lines.extend(
        [
            "",
            f"Non-whitelist types: {', '.join(non_whitelist_types) if non_whitelist_types else 'none'}",
            f"Noise-like entities: {len(noise_entities)}",
        ]
    )
    for name in noise_entities[:20]:
        lines.append(f"  - {name}")

    lines.extend(
        [
            "",
            f"Alias groups: {len(alias_groups)}",
        ]
    )
    for group in alias_groups[:20]:
        lines.append(f"  - {' | '.join(group)}")

    lines.extend(
        [
            "",
            "Top-degree nodes:",
        ]
    )
    for node_id, degree in by_degree.most_common(15):
        lines.append(f"  {degree:>3}  {node_id}")

    return "\n".join(lines)
