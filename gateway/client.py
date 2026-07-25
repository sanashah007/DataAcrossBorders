import asyncio
import time

import httpx

from gateway import config
from gateway.schemas import FederatedStudy, NodeInfo, NodeStatus


async def _fetch_node(
    client: httpx.AsyncClient, node: str
) -> tuple[list[FederatedStudy], NodeStatus]:
    base_url = config.NODES[node]["url"]
    started = time.perf_counter()
    try:
        response = await client.get(
            f"{base_url}/api/studies", timeout=config.NODE_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        raw = response.json()
    except httpx.TimeoutException as exc:
        return [], NodeStatus(
            node=node,
            base_url=base_url,
            status="timeout",
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            error=f"{type(exc).__name__}: node did not respond within "
            f"{config.NODE_TIMEOUT_SECONDS}s",
        )
    except Exception as exc:
        return [], NodeStatus(
            node=node,
            base_url=base_url,
            status="error",
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            error=f"{type(exc).__name__}: {exc}",
        )

    records = [
        FederatedStudy(
            **record,
            SourceNode=node,
            FederatedID=f"{node}:{record['StudyID']}",
        )
        for record in raw
    ]
    return records, NodeStatus(
        node=node,
        base_url=base_url,
        status="ok",
        record_count=len(records),
        latency_ms=round((time.perf_counter() - started) * 1000, 1),
    )


async def fetch_studies(
    client: httpx.AsyncClient, nodes: list[str]
) -> tuple[list[FederatedStudy], list[NodeStatus]]:
    """Fan out to the selected nodes concurrently.

    Never raises on node failure — a down or slow node becomes a NodeStatus with a
    non-ok status and the query still returns whatever the other nodes provided.
    """
    outcomes = await asyncio.gather(*(_fetch_node(client, node) for node in nodes))

    records: list[FederatedStudy] = []
    statuses: list[NodeStatus] = []
    for node_records, status in outcomes:
        records.extend(node_records)
        statuses.append(status)

    # Nodes the caller excluded still show up, so the sources block always
    # accounts for the full federation.
    for node in config.NODES:
        if node not in nodes:
            statuses.append(
                NodeStatus(
                    node=node, base_url=config.NODES[node]["url"], status="skipped"
                )
            )
    statuses.sort(key=lambda s: list(config.NODES).index(s.node))

    return records, statuses


async def _probe_node(client: httpx.AsyncClient, node: str) -> NodeInfo:
    meta = config.NODES[node]
    started = time.perf_counter()
    try:
        response = await client.get(
            f"{meta['url']}/health", timeout=config.NODE_TIMEOUT_SECONDS
        )
        response.raise_for_status()
    except Exception as exc:
        return NodeInfo(
            node=node,
            base_url=meta["url"],
            institution=meta["institution"],
            reachable=False,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            error=f"{type(exc).__name__}: {exc}",
        )
    return NodeInfo(
        node=node,
        base_url=meta["url"],
        institution=meta["institution"],
        reachable=True,
        latency_ms=round((time.perf_counter() - started) * 1000, 1),
    )


async def probe_nodes(client: httpx.AsyncClient) -> list[NodeInfo]:
    return list(await asyncio.gather(*(_probe_node(client, n) for n in config.NODES)))
