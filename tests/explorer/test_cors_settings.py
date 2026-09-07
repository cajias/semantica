#!/usr/bin/env python3
"""Tests for ``semantica.utils.cors`` -- the one place CORS origins are read.

Semantica ships two ASGI entry points that mount the same routers:
``semantica/server.py`` (the ``semantica-server`` command) and
``semantica/explorer/app.py`` (what the published Docker image runs). Both must
answer the same question -- which browser origins may talk to this deployment --
and they used to answer it from *different* environment variables. ``server.py``
read ``SEMANTICA_CORS_ORIGINS``; the Explorer read ``ALLOWED_ORIGINS``, falling
back to ``EXPLORER_CORS_ORIGINS``. Every default named localhost, so an operator
who configured the name the *other* entry point honoured got localhost-only CORS
and no log line explaining why the browser refused every request.

So the load-bearing test in this file is ``TestBothEntryPointsAgree``. It drives
the *real middleware configuration* of both applications from the same
environment and compares them. A fix that re-implemented the precedence rules
in each entry point would satisfy every other test here and fail that one.

Reading ``server.py``'s configuration honestly needs ``importlib.reload``: its
``app`` is a module-level object built at import time, so by the time a test
sets an environment variable the resolution has already happened.
``_server_options`` reloads the module and reads the keyword arguments actually
handed to ``CORSMiddleware``, rather than re-deriving them and hoping they match
what got installed.
"""

import importlib
import logging

import pytest
from fastapi.testclient import TestClient

from semantica.context.context_graph import ContextGraph
from semantica.context.snapshot import SNAPSHOT_URI_ENV
from semantica.explorer.app import create_app
from semantica.explorer.session import GraphSession
from semantica.utils import cors, security_headers

ORIGIN_ENV_NAMES = (cors.CANONICAL_ORIGINS_ENV,) + cors.DEPRECATED_ORIGINS_ENV
CREDENTIAL_ENV_NAMES = (
    cors.CANONICAL_CREDENTIALS_ENV,
) + cors.DEPRECATED_CREDENTIALS_ENV

LOCALHOST_DEFAULT = ["http://localhost:5173", "http://127.0.0.1:5173"]


@pytest.fixture(autouse=True)
def _clear_cors_env(monkeypatch):
    """Start every test from "operator configured nothing".

    Without this a name left set by an earlier test would look like deliberate
    configuration, and the default/no-warning cases would pass for the wrong
    reason.
    """
    for name in ORIGIN_ENV_NAMES + CREDENTIAL_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _cors_options(app):
    """Return the kwargs ``CORSMiddleware`` was actually constructed with.

    Deliberately reads the installed middleware instead of calling the resolver
    again: the point is to prove the resolved values reached Starlette.
    """
    for middleware in app.user_middleware:
        if getattr(middleware.cls, "__name__", "") == "CORSMiddleware":
            return dict(middleware.kwargs)
    raise AssertionError("CORSMiddleware is not installed on this app")


def _cors_log(caplog):
    """Only the resolver's own records, so unrelated warnings cannot pass a test."""
    return "\n".join(
        record.getMessage() for record in caplog.records if record.name == cors.__name__
    )


def _explorer_options():
    """CORS options of a freshly constructed Explorer app."""
    return _cors_options(create_app())


def _server_app():
    """``semantica.server``'s app, re-imported under the current environment.

    Its ``app`` is a module-level object built at import time, so a test that
    sets an environment variable has to reload the module to be read honestly.
    """
    import semantica.server

    return importlib.reload(semantica.server).app


def _server_options():
    """CORS options of ``semantica.server`` re-imported under the current env."""
    return _cors_options(_server_app())


class TestCanonicalName:
    """The namespaced name is the one the documentation points operators at."""

    def test_canonical_name_sets_the_origins(self, monkeypatch):
        """GIVEN only SEMANTICA_CORS_ORIGINS is set
        WHEN the settings are resolved
        THEN exactly those origins are allowed."""
        monkeypatch.setenv(
            cors.CANONICAL_ORIGINS_ENV,
            "https://kg.example.com,https://admin.example.com",
        )

        settings = cors.resolve_cors_settings()

        assert settings.origins == [
            "https://kg.example.com",
            "https://admin.example.com",
        ], "the canonical name must be honoured verbatim, in order"

    def test_canonical_name_reaches_both_middlewares(self, monkeypatch):
        """GIVEN only SEMANTICA_CORS_ORIGINS is set
        WHEN each entry point builds its app
        THEN both install CORSMiddleware with exactly those origins."""
        monkeypatch.setenv(cors.CANONICAL_ORIGINS_ENV, "https://kg.example.com")

        assert _explorer_options()["allow_origins"] == ["https://kg.example.com"], (
            "the Explorer must configure CORS from the canonical name -- this is "
            "the entry point the Docker image runs"
        )
        assert _server_options()["allow_origins"] == [
            "https://kg.example.com"
        ], "semantica-server must configure CORS from the canonical name too"

    def test_no_deprecation_warning_for_the_canonical_name(self, monkeypatch, caplog):
        """GIVEN the canonical name is set
        WHEN the settings are resolved
        THEN nothing is reported as deprecated."""
        monkeypatch.setenv(cors.CANONICAL_ORIGINS_ENV, "https://kg.example.com")

        with caplog.at_level(logging.WARNING, logger=cors.__name__):
            cors.resolve_cors_settings()

        assert (
            "deprecated" not in _cors_log(caplog).lower()
        ), "configuring the recommended name must not produce a warning"


class TestDeprecatedNames:
    """Both older names keep working so no shipped manifest breaks."""

    @pytest.mark.parametrize("deprecated_name", cors.DEPRECATED_ORIGINS_ENV)
    def test_deprecated_name_still_sets_the_origins(self, monkeypatch, deprecated_name):
        """GIVEN only a deprecated name is set
        WHEN the settings are resolved
        THEN its origins are still honoured."""
        monkeypatch.setenv(deprecated_name, "https://legacy.example.com")

        settings = cors.resolve_cors_settings()

        assert settings.origins == ["https://legacy.example.com"], (
            "{} must keep working -- deploy manifests and the Dockerfile have "
            "shipped it".format(deprecated_name)
        )

    @pytest.mark.parametrize("deprecated_name", cors.DEPRECATED_ORIGINS_ENV)
    def test_deprecated_name_warns_and_names_its_replacement(
        self, monkeypatch, deprecated_name, caplog
    ):
        """GIVEN only a deprecated name is set
        WHEN the settings are resolved
        THEN a warning names both the old name and the canonical replacement."""
        monkeypatch.setenv(deprecated_name, "https://legacy.example.com")

        with caplog.at_level(logging.WARNING, logger=cors.__name__):
            cors.resolve_cors_settings()

        assert deprecated_name in _cors_log(
            caplog
        ), "the warning must name the variable the operator actually set"
        assert cors.CANONICAL_ORIGINS_ENV in _cors_log(caplog), (
            "a deprecation warning that does not name the replacement leaves the "
            "operator guessing"
        )

    @pytest.mark.parametrize("deprecated_name", cors.DEPRECATED_ORIGINS_ENV)
    def test_deprecated_name_reaches_both_middlewares(
        self, monkeypatch, deprecated_name
    ):
        """GIVEN only a deprecated name is set
        WHEN each entry point builds its app
        THEN both install CORSMiddleware with those origins."""
        monkeypatch.setenv(deprecated_name, "https://legacy.example.com")

        assert _explorer_options()["allow_origins"] == ["https://legacy.example.com"]
        assert _server_options()["allow_origins"] == [
            "https://legacy.example.com"
        ], "semantica-server ignored {} before this fix".format(deprecated_name)


class TestPrecedence:
    """Every supported pair resolves deterministically, canonical first.

    The canonical name has to outrank ``ALLOWED_ORIGINS`` specifically because
    the published image bakes ``ALLOWED_ORIGINS`` into its own ``ENV``: if the
    baked-in name won, setting the canonical one on a container would silently
    do nothing.
    """

    def test_canonical_beats_allowed_origins(self, monkeypatch):
        """GIVEN both SEMANTICA_CORS_ORIGINS and ALLOWED_ORIGINS are set
        WHEN the settings are resolved
        THEN the canonical name wins."""
        monkeypatch.setenv(cors.CANONICAL_ORIGINS_ENV, "https://canonical.example")
        monkeypatch.setenv("ALLOWED_ORIGINS", "https://baked-in.example")

        assert cors.resolve_cors_settings().origins == ["https://canonical.example"]

    def test_canonical_beats_explorer_cors_origins(self, monkeypatch):
        """GIVEN both SEMANTICA_CORS_ORIGINS and EXPLORER_CORS_ORIGINS are set
        WHEN the settings are resolved
        THEN the canonical name wins."""
        monkeypatch.setenv(cors.CANONICAL_ORIGINS_ENV, "https://canonical.example")
        monkeypatch.setenv("EXPLORER_CORS_ORIGINS", "https://older.example")

        assert cors.resolve_cors_settings().origins == ["https://canonical.example"]

    def test_allowed_origins_beats_explorer_cors_origins(self, monkeypatch):
        """GIVEN both deprecated names are set and the canonical one is not
        WHEN the settings are resolved
        THEN ALLOWED_ORIGINS wins, as it did before this change."""
        monkeypatch.setenv("ALLOWED_ORIGINS", "https://baked-in.example")
        monkeypatch.setenv("EXPLORER_CORS_ORIGINS", "https://older.example")

        assert cors.resolve_cors_settings().origins == ["https://baked-in.example"], (
            "keeping the previous relative order means nobody who sets both sees "
            "their effective configuration change"
        )

    def test_canonical_beats_both_deprecated_names_together(self, monkeypatch):
        """GIVEN all three names are set
        WHEN the settings are resolved
        THEN the canonical name wins."""
        monkeypatch.setenv(cors.CANONICAL_ORIGINS_ENV, "https://canonical.example")
        monkeypatch.setenv("ALLOWED_ORIGINS", "https://baked-in.example")
        monkeypatch.setenv("EXPLORER_CORS_ORIGINS", "https://older.example")

        assert cors.resolve_cors_settings().origins == ["https://canonical.example"]

    def test_precedence_is_identical_in_both_entry_points(self, monkeypatch):
        """GIVEN all three names are set
        WHEN each entry point builds its app
        THEN both resolve the same winner."""
        monkeypatch.setenv(cors.CANONICAL_ORIGINS_ENV, "https://canonical.example")
        monkeypatch.setenv("ALLOWED_ORIGINS", "https://baked-in.example")
        monkeypatch.setenv("EXPLORER_CORS_ORIGINS", "https://older.example")

        assert _explorer_options()["allow_origins"] == ["https://canonical.example"]
        assert _server_options()["allow_origins"] == ["https://canonical.example"]


class TestDevelopmentDefault:
    """No configuration means localhost, quietly."""

    def test_default_is_the_localhost_pair(self):
        """GIVEN no CORS name is set
        WHEN the settings are resolved
        THEN the localhost development default applies unchanged."""
        assert cors.resolve_cors_settings().origins == LOCALHOST_DEFAULT

    def test_default_produces_no_warning(self, caplog):
        """GIVEN no CORS name is set
        WHEN the settings are resolved
        THEN nothing is logged -- a developer running locally is not nagged."""
        with caplog.at_level(logging.WARNING, logger=cors.__name__):
            cors.resolve_cors_settings()

        assert _cors_log(caplog) == "", (
            "the unconfigured localhost default is the correct development "
            "setup, so warning about it would train operators to ignore the log"
        )

    def test_both_entry_points_default_identically(self):
        """GIVEN no CORS name is set
        WHEN each entry point builds its app
        THEN both fall back to the same localhost pair."""
        assert _explorer_options()["allow_origins"] == LOCALHOST_DEFAULT
        assert _server_options()["allow_origins"] == LOCALHOST_DEFAULT


class TestListParsing:
    """Comma-separated parsing tolerates the whitespace humans type."""

    def test_whitespace_is_trimmed_and_empty_entries_dropped(self, monkeypatch):
        """GIVEN a value padded with spaces and containing an empty entry
        WHEN the settings are resolved
        THEN entries are trimmed and empties dropped."""
        monkeypatch.setenv(cors.CANONICAL_ORIGINS_ENV, " a , , b ")

        assert cors.resolve_cors_settings().origins == ["a", "b"], (
            "an untrimmed origin never matches a browser's Origin header, and a "
            "bare '' entry would be a permanently dead allowlist slot"
        )

    def test_explicitly_empty_value_means_no_origins(self, monkeypatch):
        """GIVEN the canonical name is set to an empty string
        WHEN the settings are resolved
        THEN no origins are allowed, rather than silently defaulting."""
        monkeypatch.setenv(cors.CANONICAL_ORIGINS_ENV, "")

        assert cors.resolve_cors_settings().origins == [], (
            "an explicitly empty value is a deliberate 'no cross-origin access' "
            "choice; falling back to localhost would override the operator"
        )


class TestWildcardOrigin:
    """``*`` is reported, and its dangerous pairing with credentials corrected."""

    def test_wildcard_is_honoured_but_warned_about(self, monkeypatch, caplog):
        """GIVEN origins are set to *
        WHEN the settings are resolved
        THEN it still applies, and a warning says every site may connect."""
        monkeypatch.setenv(cors.CANONICAL_ORIGINS_ENV, "*")

        with caplog.at_level(logging.WARNING, logger=cors.__name__):
            settings = cors.resolve_cors_settings()

        assert settings.origins == ["*"], (
            "refusing * would abort semantica.server at import, where the "
            "traceback is least legible; the operator gets a warning instead"
        )
        warning = _cors_log(caplog)
        assert (
            "*" in warning and cors.CANONICAL_ORIGINS_ENV in warning
        ), "the warning must name both the problem and the variable to fix"

    def test_wildcard_with_credentials_drops_the_credentials(self, monkeypatch, caplog):
        """GIVEN origins are * and credentialed requests are enabled
        WHEN the settings are resolved
        THEN credentials are dropped and the reason logged.

        Starlette does not refuse this pair. It answers it by reflecting the
        request's own Origin alongside ``Access-Control-Allow-Credentials:
        true``, which turns "wildcard, so no credentials" into "every site may
        make credentialed requests" -- so the combination has to be corrected,
        not merely reported.
        """
        monkeypatch.setenv(cors.CANONICAL_ORIGINS_ENV, "*")
        monkeypatch.setenv(cors.CANONICAL_CREDENTIALS_ENV, "true")

        with caplog.at_level(logging.WARNING, logger=cors.__name__):
            settings = cors.resolve_cors_settings()

        assert settings.allow_credentials is False, (
            "* plus credentials must not survive resolution -- Starlette turns "
            "it into origin reflection with credentials allowed"
        )
        assert (
            "credential" in _cors_log(caplog).lower()
        ), "silently dropping an explicit opt-in would be its own surprise"

    def test_wildcard_app_does_not_reflect_an_arbitrary_origin(self, monkeypatch):
        """GIVEN origins are * and credentialed requests are enabled
        WHEN a request arrives from an unrelated origin
        THEN the response allows * without allowing credentials."""
        monkeypatch.setenv(cors.CANONICAL_ORIGINS_ENV, "*")
        monkeypatch.setenv(cors.CANONICAL_CREDENTIALS_ENV, "true")

        with TestClient(create_app()) as client:
            response = client.get(
                "/api/health", headers={"Origin": "https://evil.example"}
            )

        assert (
            response.headers["access-control-allow-origin"] == "*"
        ), "with credentials dropped the wildcard stays a plain wildcard"
        assert "access-control-allow-credentials" not in response.headers, (
            "reflecting evil.example with credentials allowed would let any page "
            "read this deployment's responses as the logged-in user"
        )


class TestWildcardReachesTheWebSocketHandshake:
    """The WS Origin gate must read ``*`` the way the HTTP layer does.

    ``CORSMiddleware`` does not cover WebSocket handshakes, so
    ``/ws/graph-updates`` checks ``Origin`` against the same resolved list by
    hand. A literal ``"*"`` matches no real ``Origin`` header, so testing it as
    an ordinary list entry left ``SEMANTICA_CORS_ORIGINS=*`` wide open over
    HTTP while refusing *every* browser on the socket -- including the
    deployment's own page, whose live graph updates then silently never
    connect, with only a 4403 close to show for it.
    """

    def test_wildcard_accepts_an_arbitrary_origin(self, monkeypatch):
        """GIVEN origins are set to *
        WHEN a handshake arrives from an unrelated origin
        THEN it is accepted, as the HTTP layer already accepts it."""
        monkeypatch.setenv(cors.CANONICAL_ORIGINS_ENV, "*")

        with TestClient(create_app()) as client:
            with client.websocket_connect(
                "/ws/graph-updates", headers={"Origin": "https://anywhere.example"}
            ) as websocket:
                ack = websocket.receive_json()

        assert ack["event"] == "connection_ack", (
            "the wildcard is honoured for HTTP, so refusing it here makes the "
            "Explorer's own page unable to open its socket"
        )

    def test_a_concrete_list_still_rejects_a_foreign_origin(self, monkeypatch):
        """GIVEN origins name one concrete host
        WHEN a handshake arrives from another origin
        THEN it is still refused (GHSA-4643-wpgq-w329)."""
        monkeypatch.setenv(cors.CANONICAL_ORIGINS_ENV, "https://kg.example.com")

        with TestClient(create_app()) as client:
            with pytest.raises(Exception):
                with client.websocket_connect(
                    "/ws/graph-updates", headers={"Origin": "https://evil.example"}
                ):
                    pass


class TestPreloadedGraphOutranksTheSnapshot:
    """``--graph`` must not be silently replaced by ``SEMANTICA_SNAPSHOT_URI``.

    ``semantica-explorer --graph`` loads the file, prints its node and edge
    counts, and hands the session to ``create_app``. The lifespan then used to
    restore the snapshot over it unconditionally, so the operator was told
    "loaded N nodes" and served something else entirely.
    """

    def test_a_preloaded_graph_is_not_clobbered_and_the_skip_is_logged(
        self, tmp_path, monkeypatch, caplog
    ):
        """GIVEN a session whose graph is already populated and a snapshot URI
        WHEN the app starts
        THEN the snapshot is not restored and a warning names the URI."""
        snapshot = ContextGraph(advanced_analytics=False)
        snapshot.add_node("from_snapshot", node_type="concept", content="Ambient")
        path = tmp_path / "snapshot.json"
        snapshot.save_to_file(str(path))
        monkeypatch.setenv(SNAPSHOT_URI_ENV, str(path))

        preloaded = ContextGraph(advanced_analytics=False)
        preloaded.add_node("from_cli", node_type="concept", content="--graph")
        app = create_app(session=GraphSession(preloaded))

        with caplog.at_level(logging.WARNING):
            with TestClient(app) as client:
                nodes = client.get("/api/graph/nodes").json()

        assert {node["id"] for node in nodes["nodes"]} == {"from_cli"}, (
            "the graph the operator passed on the command line was replaced by "
            "ambient snapshot state"
        )
        assert SNAPSHOT_URI_ENV in caplog.text and str(path) in caplog.text, (
            "overriding the configured snapshot silently leaves the operator "
            "with no way to explain which graph is being served"
        )

    def test_an_empty_session_graph_still_restores(self, tmp_path, monkeypatch):
        """GIVEN a session with an empty graph and a snapshot URI
        WHEN the app starts
        THEN the snapshot is restored exactly as before."""
        snapshot = ContextGraph(advanced_analytics=False)
        snapshot.add_node("from_snapshot", node_type="concept", content="Ambient")
        path = tmp_path / "snapshot.json"
        snapshot.save_to_file(str(path))
        monkeypatch.setenv(SNAPSHOT_URI_ENV, str(path))

        app = create_app(session=GraphSession(ContextGraph(advanced_analytics=False)))

        with TestClient(app) as client:
            nodes = client.get("/api/graph/nodes").json()

        assert {node["id"] for node in nodes["nodes"]} == {
            "from_snapshot"
        }, "no --graph was passed, so the snapshot is the only graph there is"


class TestCredentials:
    """One credentials flag, one default, both entry points."""

    def test_credentials_are_off_by_default(self):
        """GIVEN no credentials name is set
        WHEN the settings are resolved
        THEN credentialed cross-origin requests are not allowed."""
        assert cors.resolve_cors_settings().allow_credentials is False, (
            "X-API-Key is a header, not a cookie, so credentials buy nothing "
            "here while widening cross-site exposure"
        )

    @pytest.mark.parametrize("credentials_name", CREDENTIAL_ENV_NAMES)
    def test_credentials_can_be_opted_into_by_either_name(
        self, monkeypatch, credentials_name
    ):
        """GIVEN either credentials name is set to true
        WHEN the settings are resolved
        THEN credentialed requests are allowed."""
        monkeypatch.setenv(credentials_name, "true")

        assert cors.resolve_cors_settings().allow_credentials is True

    def test_deprecated_credentials_name_warns(self, monkeypatch, caplog):
        """GIVEN the Explorer-prefixed credentials name is set
        WHEN the settings are resolved
        THEN a warning names the canonical replacement."""
        monkeypatch.setenv("EXPLORER_CORS_CREDENTIALS", "true")

        with caplog.at_level(logging.WARNING, logger=cors.__name__):
            cors.resolve_cors_settings()

        assert cors.CANONICAL_CREDENTIALS_ENV in _cors_log(
            caplog
        ), "the name is misleading once semantica-server honours it too"

    def test_server_no_longer_hardcodes_credentials_on(self, monkeypatch):
        """GIVEN no credentials name is set
        WHEN semantica.server builds its app
        THEN credentials are off, matching the Explorer.

        server.py used to pass ``allow_credentials=True`` unconditionally while
        sharing the same origins list, making it strictly more permissive than
        the Explorer for the same configuration.
        """
        monkeypatch.setenv(cors.CANONICAL_ORIGINS_ENV, "https://kg.example.com")

        assert _server_options()["allow_credentials"] is False

    def test_credentials_opt_in_reaches_both_middlewares(self, monkeypatch):
        """GIVEN credentials are enabled by the canonical name
        WHEN each entry point builds its app
        THEN both install CORSMiddleware with credentials allowed."""
        monkeypatch.setenv(cors.CANONICAL_ORIGINS_ENV, "https://kg.example.com")
        monkeypatch.setenv(cors.CANONICAL_CREDENTIALS_ENV, "true")

        assert _explorer_options()["allow_credentials"] is True
        assert _server_options()["allow_credentials"] is True


class TestBothEntryPointsAgree:
    """The drift guard: this is the test the original bug would have failed.

    It compares the middleware configuration the two applications actually
    install, across the environments that used to disagree. Re-implementing the
    precedence rules separately in each entry point passes the rest of this file
    and fails here.
    """

    @pytest.mark.parametrize(
        "environment",
        [
            {},
            {"SEMANTICA_CORS_ORIGINS": "https://kg.example.com"},
            {"ALLOWED_ORIGINS": "https://baked-in.example"},
            {"EXPLORER_CORS_ORIGINS": "https://older.example"},
            {
                "SEMANTICA_CORS_ORIGINS": "https://canonical.example",
                "ALLOWED_ORIGINS": "https://baked-in.example",
                "EXPLORER_CORS_ORIGINS": "https://older.example",
            },
            {
                "SEMANTICA_CORS_ORIGINS": " a , , b ",
                "SEMANTICA_CORS_CREDENTIALS": "true",
            },
            {"SEMANTICA_CORS_ORIGINS": "*", "EXPLORER_CORS_CREDENTIALS": "true"},
        ],
        ids=[
            "unconfigured",
            "canonical-only",
            "allowed-origins-only",
            "explorer-cors-origins-only",
            "all-three-names",
            "whitespace-plus-credentials",
            "wildcard-plus-credentials",
        ],
    )
    def test_middleware_configuration_matches(self, monkeypatch, environment):
        """GIVEN an environment
        WHEN both entry points build their apps from it
        THEN their CORS origins and credentials flag are identical."""
        for name, value in environment.items():
            monkeypatch.setenv(name, value)

        explorer = _explorer_options()
        server = _server_options()

        assert explorer["allow_origins"] == server["allow_origins"], (
            "the two entry points disagree about allowed origins for {} -- one "
            "of them is reading a name the other ignores".format(environment)
        )
        assert explorer["allow_credentials"] == server["allow_credentials"], (
            "the two entry points disagree about credentialed requests for {} -- "
            "the more permissive one is an unintended hole".format(environment)
        )

    def test_both_entry_points_use_the_shared_resolver(self, monkeypatch):
        """GIVEN the shared resolver is replaced
        WHEN each entry point builds its app
        THEN both reflect the replacement.

        Asserting on values alone can be satisfied by two copy-pasted readers
        that happen to agree. Patching the resolver proves both call it, so the
        duplication cannot come back.
        """
        sentinel = cors.CorsSettings(
            origins=["https://sentinel.example"], allow_credentials=True
        )
        monkeypatch.setattr(cors, "resolve_cors_settings", lambda: sentinel)

        assert _explorer_options()["allow_origins"] == ["https://sentinel.example"], (
            "semantica/explorer/app.py resolves CORS itself instead of calling "
            "semantica.utils.cors.resolve_cors_settings"
        )
        assert _server_options()["allow_origins"] == ["https://sentinel.example"], (
            "semantica/server.py resolves CORS itself instead of calling "
            "semantica.utils.cors.resolve_cors_settings"
        )


def _response_headers(app, path, **kwargs):
    """Headers of a GET to *path*, without running the app's lifespan.

    No ``with``: neither health route needs lifespan state, and skipping it
    keeps the snapshot machinery out of a middleware test.
    """
    return TestClient(app).get(path, **kwargs).headers


class TestSecurityHeadersReachBothEntryPoints:
    """The same paired-edit hazard as CORS, one middleware over.

    ``semantica/explorer/app.py`` is what the Docker image and the App Runner
    service run, so headers installed only on ``semantica/server.py`` are
    headers the internet-facing app does not send.
    """

    def test_both_apps_send_the_static_headers(self):
        """GIVEN each entry point's app
        WHEN a request is served
        THEN both carry every shared security header."""
        explorer = _response_headers(create_app(), "/api/health")
        server = _response_headers(_server_app(), "/health")

        for name, value in security_headers.STATIC_SECURITY_HEADERS.items():
            assert explorer[name] == value, (
                "semantica/explorer/app.py does not send {} -- this is the app "
                "the container and App Runner run".format(name)
            )
            assert server[name] == value, "semantica-server does not send {}".format(
                name
            )

    @pytest.mark.parametrize("entry_point", ["explorer", "server"])
    def test_hsts_follows_the_client_facing_scheme(self, entry_point):
        """GIVEN a request behind a TLS-terminating proxy
        WHEN it is served over plain HTTP
        THEN HSTS is sent on the forwarded-https request and only that one.

        ``request.url.scheme`` is ``http`` on every deployed request, App
        Runner included, so keying HSTS off it alone never fires.
        """
        if entry_point == "explorer":
            app, path = create_app(), "/api/health"
        else:
            app, path = _server_app(), "/health"

        plain = _response_headers(app, path)
        forwarded = _response_headers(app, path, headers={"X-Forwarded-Proto": "https"})

        assert security_headers.HSTS_HEADER not in plain, (
            "HSTS on a plain-HTTP response is ignored by browsers and hides a "
            "misconfigured proxy"
        )
        assert (
            forwarded[security_headers.HSTS_HEADER] == security_headers.HSTS_VALUE
        ), "HSTS never fires behind a TLS terminator if it only reads url.scheme"
