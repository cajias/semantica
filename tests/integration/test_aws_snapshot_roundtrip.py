"""Container-restart round-trip harness for the Explorer graph snapshot.

Answers one question the codebase currently has no way to observe: does the
knowledge graph survive a container restart?

These tests are EXPECTED TO BE RED until the snapshot work lands (atomic
write, snapshot store, startup restore, interval writer, shutdown snapshot).
The Explorer keeps its graph in an in-memory ``ContextGraph`` owned by
``GraphSession``, so today the graph dies with the process and nothing ever
reads or writes ``SEMANTICA_SNAPSHOT_URI``. Being honestly red is the point.

Every test runs against the real image through ``docker compose``, under an
isolated compose project name so a failed run can never remove a developer's
own containers or volumes.
"""

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

# Isolated from the default `semantica` project a developer would use, so the
# `down -v` teardown below only ever destroys this harness's own volume.
COMPOSE_PROJECT = "semantica-snapshot-roundtrip"

BASE_URL = "http://127.0.0.1:8000"
API_KEY = "local-dev-key"
SNAPSHOT_INTERVAL = 2  # seconds; the code default of 30 would stall the suite

# `up --build` builds the whole image on first run: an `npm ci` + Vite build in
# node:26-alpine, then `pip install .[explorer]`. That is minutes, not seconds.
BUILD_TIMEOUT = 1800
HEALTH_TIMEOUT = 180
DOCKER_PROBE_TIMEOUT = 60

NODE_ID = "roundtrip-source"
TARGET_ID = "roundtrip-target"
EDGE_TYPE = "relates_to"


def _compose_env():
    """Env for compose: the API key and a short snapshot interval."""
    return {
        **os.environ,
        "COMPOSE_PROJECT_NAME": COMPOSE_PROJECT,
        "SEMANTICA_API_KEY": API_KEY,
        "SEMANTICA_SNAPSHOT_INTERVAL": str(SNAPSHOT_INTERVAL),
    }


def _compose(*args, timeout=BUILD_TIMEOUT, check=True):
    """Run `docker compose` against the repo-root compose file."""
    result = subprocess.run(
        ["docker", "compose", "-f", str(REPO_ROOT / "docker-compose.yml"), *args],
        cwd=str(REPO_ROOT),
        env=_compose_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            "`docker compose {}` failed with exit {}.\nstdout:\n{}\nstderr:\n{}".format(
                " ".join(args), result.returncode, result.stdout, result.stderr
            )
        )
    return result


def _skip_unless_docker_usable():
    """Skip -- never fail -- when the environment cannot run containers."""
    if shutil.which("docker") is None:
        pytest.skip("docker is not on PATH")
    for probe, reason in (
        (["docker", "info"], "the docker daemon is unreachable"),
        (["docker", "compose", "version"], "docker compose v2 is unavailable"),
    ):
        try:
            if subprocess.run(
                probe, capture_output=True, timeout=DOCKER_PROBE_TIMEOUT
            ).returncode:
                pytest.skip("Cannot run the snapshot harness: {}".format(reason))
        except (OSError, subprocess.TimeoutExpired):
            pytest.skip("Cannot run the snapshot harness: {}".format(reason))
    with socket.socket() as probe_socket:
        probe_socket.settimeout(2)
        if probe_socket.connect_ex(("127.0.0.1", 8000)) == 0:
            pytest.skip("host port 8000 is already in use; the harness binds it")


def _start_explorer():
    """Bring up only the Explorer, waiting until it reports healthy.

    `--no-deps` skips the falkordb service on purpose: the Explorer reads
    FALKORDB_* into settings but never opens that connection (see the comment
    in semantica/explorer/app.py), so starting it would only cost an image
    pull.
    """
    _compose("up", "-d", "--build", "--no-deps", "explorer")
    _wait_for_health()


def _wait_for_health(timeout=HEALTH_TIMEOUT):
    """Poll /api/health (unauthenticated) until it reports ok."""
    deadline = time.monotonic() + timeout
    last_error = "never polled"
    while time.monotonic() < deadline:
        try:
            response = httpx.get("{}/api/health".format(BASE_URL), timeout=5)
            if response.status_code == 200 and response.json().get("status") == "ok":
                return
            last_error = "HTTP {} {}".format(response.status_code, response.text[:200])
        except (httpx.HTTPError, ValueError) as exc:
            last_error = "{}: {}".format(type(exc).__name__, exc)
        time.sleep(2)
    logs = _compose("logs", "--tail", "50", "explorer", timeout=60, check=False)
    raise AssertionError(
        "Explorer never became healthy within {}s. Last probe: {}\n"
        "This is a harness/startup problem, not a persistence result.\n"
        "Container logs:\n{}".format(timeout, last_error, logs.stdout)
    )


def _write_graph():
    """Write a node pair and an edge through the real REST API.

    `POST /api/import` is the only route that creates nodes and edges; the
    /api/graph routes are read-only.
    """
    payload = {
        "nodes": [
            {"id": NODE_ID, "type": "entity", "metadata": {"probe": "roundtrip"}},
            {"id": TARGET_ID, "type": "entity"},
        ],
        "edges": [{"source": NODE_ID, "target": TARGET_ID, "type": EDGE_TYPE}],
    }
    response = httpx.post(
        "{}/api/import".format(BASE_URL),
        files={"file": ("graph.json", json.dumps(payload), "application/json")},
        headers={"X-API-Key": API_KEY},
        timeout=60,
    )
    assert response.status_code == 200, (
        "Seeding the graph through POST /api/import failed with HTTP {}: {}. "
        "A 503 here means SEMANTICA_API_KEY never reached the container, so "
        "the harness is broken -- this says nothing about persistence.".format(
            response.status_code, response.text[:300]
        )
    )
    return response.json()


def _read_graph():
    """Read nodes and edges back, returning (node_ids, edge_pairs)."""
    headers = {"X-API-Key": API_KEY}
    nodes = httpx.get(
        "{}/api/graph/nodes".format(BASE_URL), headers=headers, timeout=30
    )
    edges = httpx.get(
        "{}/api/graph/edges".format(BASE_URL), headers=headers, timeout=30
    )
    for label, response in (("nodes", nodes), ("edges", edges)):
        assert response.status_code == 200, (
            "Reading {} back failed with HTTP {}: {}. A 503 means the API key "
            "did not reach the container -- a harness fault, not a persistence "
            "result.".format(label, response.status_code, response.text[:300])
        )
    node_ids = {node["id"] for node in nodes.json()["nodes"]}
    edge_pairs = {
        (edge["source"], edge["target"], edge["type"]) for edge in edges.json()["edges"]
    }
    return node_ids, edge_pairs


@pytest.fixture
def explorer_stack():
    """A freshly composed Explorer container on an empty snapshot volume.

    Function-scoped and volume-clean on both sides so the tests do not depend
    on each other's ordering: the empty-graph test needs a volume with no
    snapshot on it, which the round-trip test would otherwise have populated.
    """
    _skip_unless_docker_usable()
    _compose("down", "-v", timeout=300, check=False)  # drop any leftover volume
    try:
        _start_explorer()
        yield
    finally:
        _compose("down", "-v", timeout=300, check=False)


def test_graph_survives_container_restart(explorer_stack):
    """GIVEN a freshly composed container serving the Explorer,
    WHEN a node and an edge are written through the REST API and the
    container is then restarted,
    THEN the same node and edge are still present after the restart.
    """
    imported = _write_graph()
    assert imported["nodes_added"] == 2 and imported["edges_added"] == 1, (
        "The seed import did not report the expected counts: {}. The harness "
        "cannot test survival of data it never wrote.".format(imported)
    )

    nodes_before, edges_before = _read_graph()
    assert NODE_ID in nodes_before, (
        "The seeded node is missing BEFORE any restart, so the write path "
        "itself is broken. Saw nodes: {}".format(sorted(nodes_before))
    )
    assert (NODE_ID, TARGET_ID, EDGE_TYPE) in edges_before, (
        "The seeded edge is missing BEFORE any restart, so the write path "
        "itself is broken. Saw edges: {}".format(sorted(edges_before))
    )

    # Snapshot moment under test: the INTERVAL WRITER. Waiting longer than
    # SEMANTICA_SNAPSHOT_INTERVAL means the snapshot must already be on disk
    # before the container goes away, so this assertion does not silently
    # depend on the shutdown snapshot instead.
    time.sleep(SNAPSHOT_INTERVAL * 2 + 1)

    # `down` (no -v) then `up` -- not `restart`. This destroys the container
    # and builds a new one against the same named volume, which is what an
    # image redeploy does. `restart` would reuse the container filesystem and
    # so could pass even if the snapshot never left the container.
    _compose("down", timeout=300)
    _start_explorer()

    nodes_after, edges_after = _read_graph()
    assert NODE_ID in nodes_after, (
        "Node {!r} did not survive container recreation. The graph came back "
        "with {} node(s): {}. Either no snapshot was written to "
        "SEMANTICA_SNAPSHOT_URI or startup never restored it.".format(
            NODE_ID, len(nodes_after), sorted(nodes_after)
        )
    )
    assert (NODE_ID, TARGET_ID, EDGE_TYPE) in edges_after, (
        "Edge {!r} -> {!r} did not survive container recreation. The graph "
        "came back with {} edge(s): {}. Node restore without edge restore "
        "means the snapshot is dropping relationships.".format(
            NODE_ID, TARGET_ID, len(edges_after), sorted(edges_after)
        )
    )


def test_starts_with_empty_graph_when_no_snapshot_exists(explorer_stack):
    """GIVEN no snapshot object exists,
    WHEN the container starts,
    THEN it serves traffic with an empty graph rather than failing.
    """
    # Snapshot moment under test: STARTUP RESTORE on a first-ever deploy. A
    # missing snapshot must read as "empty graph", never as a fatal error --
    # the fixture already proved the container reached a healthy /api/health.
    nodes, edges = _read_graph()
    assert nodes == set() and edges == set(), (
        "A container started against an empty snapshot volume should serve an "
        "empty graph, but it came up with {} node(s) {} and {} edge(s) {}. "
        "Either the volume was not clean or startup restored stale "
        "state.".format(len(nodes), sorted(nodes), len(edges), sorted(edges))
    )
