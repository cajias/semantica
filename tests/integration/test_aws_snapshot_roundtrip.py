"""Container-restart round-trip harness for the Explorer graph snapshot.

Answers the one question no in-process test can: does the knowledge graph
survive losing the container it lived in? The Explorer keeps its graph in an
in-memory ``ContextGraph`` owned by ``GraphSession``, so durability rests
entirely on ``SEMANTICA_SNAPSHOT_URI`` -- and the unit tests drive that through
a lifespan in the same process, never through a real image on a real volume.

One test per snapshot moment:

* ``test_graph_survives_container_restart`` seeds a node pair and an edge
  through ``POST /api/import``, destroys the container while keeping the named
  volume, brings a new one up and reads both back. The graceful ``down`` sends
  SIGTERM, so the shutdown snapshot is the moment this proves end to end.
* ``test_interval_writer_survives_sigkill`` isolates the interval writer by
  denying the shutdown path: it waits for a tick to put the snapshot object on
  the volume, writes a second node pair, then SIGKILLs the container and
  asserts exit 137. The pre-tick data must come back and the post-tick data
  must not -- HLD section 5's bounded loss, asserted rather than tolerated.
* ``test_starts_with_empty_graph_when_no_snapshot_exists`` covers startup
  restore on a first-ever deploy: an empty volume must yield a healthy
  container serving an empty graph, never a fatal error.

Every test runs against the real image through ``docker compose``, under an
isolated compose project name so a failed run can never remove a developer's
own containers or volumes. The prerequisites -- docker on PATH, a reachable
daemon, compose v2, and a free host port 8000 -- are probed and *skip* rather
than fail: a machine that cannot run containers has learned nothing about
persistence, so a red result there would be noise.
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

# Written after the last interval tick in the SIGKILL test, so it is expected to
# be LOST -- that bounded loss is the guarantee, not a defect.
LATE_NODE_ID = "post-tick-source"
LATE_TARGET_ID = "post-tick-target"

# Longer than SNAPSHOT_INTERVAL on purpose. The SIGKILL test syncs to a tick and
# then races the next one; a 6s interval leaves seconds of margin for the write
# plus the kill, where 2s would leave a coin flip.
KILL_INTERVAL = 6


def _compose_env(interval=SNAPSHOT_INTERVAL):
    """Env for compose: the API key and a short snapshot interval."""
    return {
        **os.environ,
        "COMPOSE_PROJECT_NAME": COMPOSE_PROJECT,
        "SEMANTICA_API_KEY": API_KEY,
        "SEMANTICA_SNAPSHOT_INTERVAL": str(interval),
    }


def _compose(*args, timeout=BUILD_TIMEOUT, check=True, interval=SNAPSHOT_INTERVAL):
    """Run `docker compose` against the repo-root compose file."""
    result = subprocess.run(
        ["docker", "compose", "-f", str(REPO_ROOT / "docker-compose.yml"), *args],
        cwd=str(REPO_ROOT),
        env=_compose_env(interval),
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


def _start_explorer(interval=SNAPSHOT_INTERVAL):
    """Bring up only the Explorer, waiting until it reports healthy.

    `--no-deps` skips the falkordb service on purpose: the Explorer reads
    FALKORDB_* into settings but never opens that connection (see the comment
    in semantica/explorer/app.py), so starting it would only cost an image
    pull.
    """
    _compose("up", "-d", "--build", "--no-deps", "explorer", interval=interval)
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


def _write_graph(node_id=NODE_ID, target_id=TARGET_ID):
    """Write a node pair and an edge through the real REST API.

    `POST /api/import` is the only route that creates nodes and edges; the
    /api/graph routes are read-only.
    """
    payload = {
        "nodes": [
            {"id": node_id, "type": "entity", "metadata": {"probe": "roundtrip"}},
            {"id": target_id, "type": "entity"},
        ],
        "edges": [{"source": node_id, "target": target_id, "type": EDGE_TYPE}],
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


def _snapshot_exists():
    """True once the snapshot object is on the volume.

    Read from inside the container: the named volume has no host path the test
    can portably stat, and `python` is the one interpreter the runtime image is
    guaranteed to have.
    """
    probe = _compose(
        "exec",
        "-T",
        "explorer",
        "python",
        "-c",
        "import os,sys;sys.exit(0 if os.path.exists("
        "os.environ['SEMANTICA_SNAPSHOT_URI']) else 1)",
        timeout=60,
        check=False,
    )
    return probe.returncode == 0


def _wait_for_snapshot(timeout=90):
    """Block until the interval writer has produced the snapshot object.

    Synchronising on the write instead of guessing the writer's phase is what
    makes the bounded-loss assertion below deterministic: returning here means
    a tick has just fired, so a near-full interval remains before the next one.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _snapshot_exists():
            return
        time.sleep(0.5)
    raise AssertionError(
        "The interval writer never created the snapshot object within {}s. "
        "Nothing wrote SEMANTICA_SNAPSHOT_URI while the container was "
        "running and healthy.".format(timeout)
    )


def _explorer_exit_code():
    """Exit code of the stopped explorer container."""
    container = _compose("ps", "-aq", "explorer", timeout=60).stdout.strip()
    assert container, "No explorer container to inspect; the harness lost track of it."
    inspected = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.ExitCode}}", container.splitlines()[0]],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return int(inspected.stdout.strip())


@pytest.fixture
def explorer_stack(request):
    """A freshly composed Explorer container on an empty snapshot volume.

    Function-scoped and volume-clean on both sides so the tests do not depend
    on each other's ordering: the empty-graph test needs a volume with no
    snapshot on it, which the round-trip test would otherwise have populated.

    Indirect-parameterise to override the snapshot interval for one test.
    """
    interval = getattr(request, "param", SNAPSHOT_INTERVAL)
    _skip_unless_docker_usable()
    _compose("down", "-v", timeout=300, check=False)  # drop any leftover volume
    try:
        _start_explorer(interval)
        yield interval
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

    # What this proves: the graph survives container recreation by SOME
    # snapshot moment -- not which one. Sleeping past the interval means the
    # interval writer *should* have run, but the assertion only inspects
    # post-restart state, and the graceful `down` below sends SIGTERM, so the
    # shutdown snapshot satisfies it equally. The graceful path is in fact the
    # one this test exercises end to end; the interval writer is isolated by
    # test_interval_writer_survives_sigkill, which denies the shutdown path.
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


@pytest.mark.parametrize("explorer_stack", [KILL_INTERVAL], indirect=True)
def test_interval_writer_survives_sigkill(explorer_stack):
    """GIVEN a container with a short SEMANTICA_SNAPSHOT_INTERVAL,
    WHEN a node and edge are written, the interval is allowed to elapse, and
    the container is then SIGKILLed rather than stopped gracefully,
    THEN the graph is still present after the container is recreated.
    """
    # This is the abrupt-host-failure row of HLD section 5's failure table, and
    # it is the only one of the three snapshot moments provable in isolation:
    # SIGKILL gives uvicorn no chance to run its lifespan teardown, so
    # SnapshotService.stop() never executes and no shutdown snapshot can exist.
    # Startup restore and the shutdown snapshot cannot be isolated the same way
    # -- restore is required by every test that reads data back, and denying the
    # interval writer would mean an interval longer than the test.
    _write_graph()

    # Synchronise on the writer rather than sleeping a guessed amount. Returning
    # means a tick just fired, so the whole node/edge set above is durable AND a
    # near-full interval remains before the next tick -- which is what makes the
    # bounded-loss assertion below deterministic rather than a race.
    _wait_for_snapshot()

    # Written after the last tick, so it is inside the loss window the design
    # budgets for. Not a bug: HLD section 5 accepts losing up to one interval of
    # edits on an abrupt kill, and that is precisely what is asserted below.
    _write_graph(LATE_NODE_ID, LATE_TARGET_ID)

    _compose("kill", "explorer", timeout=120)  # v2.29.7 defaults to SIGKILL

    # How we know the graceful path did not run: 137 is 128+SIGKILL(9). A
    # SIGTERM shutdown would exit 0. There is no shutdown-snapshot log line to
    # grep for as a cross-check -- SnapshotService.stop() logs only on failure
    # or a slow thread join -- so the exit code is the evidence, not a log.
    exit_code = _explorer_exit_code()
    assert exit_code == 137, (
        "Expected exit 137 (128+SIGKILL) to prove the container died abruptly, "
        "but it exited {}. A graceful exit means the shutdown snapshot could "
        "have run, so this test would no longer isolate the interval "
        "writer.".format(exit_code)
    )

    _compose("down", timeout=300)  # keep the volume, drop the killed container
    _start_explorer(KILL_INTERVAL)

    nodes_after, edges_after = _read_graph()
    assert NODE_ID in nodes_after, (
        "Node {!r} was snapshotted by the interval writer while the container "
        "ran, but did not come back after SIGKILL. The graph returned {} "
        "node(s): {}. Durability here rests only on the interval writer -- the "
        "shutdown snapshot was denied -- so this means the interval writer is "
        "not carrying data between snapshots, and HLD section 5's "
        "bounded-loss claim does not hold.".format(
            NODE_ID, len(nodes_after), sorted(nodes_after)
        )
    )
    assert (NODE_ID, TARGET_ID, EDGE_TYPE) in edges_after, (
        "Edge {!r} -> {!r} did not survive SIGKILL although its nodes did. The "
        "interval writer is dropping relationships. Saw: {}".format(
            NODE_ID, TARGET_ID, sorted(edges_after)
        )
    )
    assert LATE_NODE_ID not in nodes_after, (
        "Node {!r} was written after the last interval tick and should have "
        "been lost to SIGKILL, but it came back. Either the shutdown snapshot "
        "ran after all -- meaning this test does not isolate the interval "
        "writer -- or a tick fired in the sub-second window before the kill, "
        "which would make this assertion racy. Saw: {}".format(
            LATE_NODE_ID, sorted(nodes_after)
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
