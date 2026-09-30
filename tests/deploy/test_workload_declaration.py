"""`ops/dashboard/workload.declaration.json` -- the file another repository reads to decide what
this workload's pause button touches.

Nothing in this repository imports it, and the repository that does compile it does not run this
repository's CI. So without these tests the declaration can drift from the units sitting beside
it in `deploy/quadlet/`, and the way that drift presents is a pause that stops the wrong thing:
a timer left running fires a fetch into a database the web app has already released, and the
dashboard still reports the workload stopped.

Two separate jobs here. First, that the declaration keeps its shape: names the workload the
binding expects, covers every controllable unit exactly once, and asks for the readiness check
the app actually serves. Second, that it stays *logical* -- the dashboard treats this file as
untrusted input and rejects anything naming a host resource, but that rejection happens over
there, at deploy time, so the same rule is asserted here where a push can catch it.
"""
from __future__ import annotations

import configparser
import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DECLARATION = _REPO_ROOT / "ops" / "dashboard" / "workload.declaration.json"
_QUADLET_DIR = _REPO_ROOT / "deploy" / "quadlet"

# Everything a declaration is not allowed to know: unit names, unit suffixes, the process
# manager, commands, secrets, and host addresses. `/readyz` is the only path it may carry.
FORBIDDEN = (
    ".service",
    ".container",
    ".timer",
    ".path",
    ".volume",
    "beehive",
    "systemctl",
    "podman",
    "Exec",
    "exec",
    "command",
    "secret",
    "Secret",
    "127.0.0.1",
    "shunlyu.com",
    "/data",
)

ACTIONABLE_ROLES = ["jobs-worker", "research-worker", "web"]


def _declaration() -> dict:
    return json.loads(DECLARATION.read_text())


def _has_install(unit: Path) -> bool:
    """Whether a Quadlet container is boot-wanted. `[Install]` is what separates the always-on
    units the dashboard starts and stops from the one-shots it only waits on."""
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read(unit)
    return "Install" in parser


def _strings(node: object) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [value for item in node.values() for value in _strings(item)]
    if isinstance(node, list):
        return [value for item in node for value in _strings(item)]
    return []


def test_the_declaration_identifies_this_workload():
    """`metadata.name` is the join between this file and the trusted binding. A rename here
    silently detaches the workload from its lifecycle policy rather than failing loudly."""
    document = _declaration()
    assert document["apiVersion"] == "dash.shunlyu.com/v1alpha1"
    assert document["kind"] == "WorkloadDeclaration"
    assert document["metadata"] == {"name": "news", "displayName": "News Center"}


def test_the_declaration_uses_lifecycle_v1_because_nothing_is_wait_only():
    """Lifecycle v2 exists for one-shot jobs the dashboard must wait for rather than stop. Since
    ADR-0012 every job runs inside an always-on worker that drains itself on SIGTERM (it finishes
    or hands back its claims), so a plain stop is the right pause and v1 is enough."""
    lifecycle = _declaration()["spec"]["capabilities"]["core.lifecycle"]
    assert lifecycle == {"version": 1, "roles": ["web", "research-worker", "jobs-worker"]}


def test_the_declaration_names_every_process_the_dashboard_must_stop():
    """The web app and the two workers. A role left out keeps working through a pause -- the
    jobs worker would still fetch into a database the web app has already released, and the
    dashboard would report a workload that is quietly still doing work."""
    lifecycle = _declaration()["spec"]["capabilities"]["core.lifecycle"]
    assert sorted(lifecycle["roles"]) == ACTIONABLE_ROLES


def test_every_role_has_one_always_on_unit_and_nothing_else_needs_control():
    """The count is the check: three always-on containers in `deploy/quadlet/` and no timers,
    path units or one-shot containers. Adding a fourth unit without a role is the drift this
    catches."""
    containers = list(_QUADLET_DIR.glob("*.container"))
    assert all(_has_install(path) for path in containers)
    assert not list(_QUADLET_DIR.glob("*.timer")) and not list(_QUADLET_DIR.glob("*.path"))
    lifecycle = _declaration()["spec"]["capabilities"]["core.lifecycle"]
    assert len(lifecycle["roles"]) == len(containers)


def test_the_declaration_asks_for_the_readiness_check_the_app_serves():
    """`/readyz`, and only `/readyz`. The app has no liveness-only endpoint to confuse it with,
    and 5 seconds is the budget the probe's own busy timeout is set under."""
    health = _declaration()["spec"]["capabilities"]["core.health"]
    assert health["version"] == 1
    assert health["checks"] == [
        {
            "name": "ready",
            "kind": "http",
            "port": "http",
            "path": "/readyz",
            "expectStatus": 200,
            "timeoutSeconds": 5,
        }
    ]


def test_the_readiness_path_is_a_route_the_web_app_registers(tmp_path):
    """Ties the declared path to the running app. A probe pointed at a path nothing serves gets
    404 forever, and the workload never reports ready after a resume."""
    from beehive.web.app import create_app

    app = create_app(str(tmp_path / "routes.db"))
    assert "/readyz" in {getattr(route, "path", None) for route in app.routes}


def test_the_declared_port_is_the_port_the_web_container_listens_on():
    """A logical alias and a container port; the host side belongs to the binding. Drift here
    means the readiness probe checks a port nothing serves."""
    ports = _declaration()["spec"]["ports"]
    assert ports == {"http": {"containerPort": 8000}}
    assert "PublishPort=127.0.0.1:8095:8000" in (
        _QUADLET_DIR / "beehive-web.container").read_text()


def test_the_declaration_names_no_host_resource():
    """The trust rule the dashboard enforces, asserted on this side of the seam. `apiVersion` is
    excluded because it is the contract's own identifier and is expected to name the dashboard."""
    document = _declaration()
    body = json.dumps({key: document[key] for key in ("metadata", "spec")})
    for fragment in FORBIDDEN:
        assert fragment not in body, f"the declaration names {fragment!r}"


def test_the_declaration_holds_no_filesystem_path():
    """`/readyz` is a request path. Anything else starting with a slash is a host detail."""
    paths = [
        value for value in _strings(_declaration())
        if value.startswith("/") or value.startswith("~")
    ]
    assert paths == ["/readyz"]


def test_the_declaration_carries_no_unexpected_top_level_keys():
    """The compiler rejects unknown fields outright, so an extra key is not a warning over
    there -- it is a workload that fails to compile and loses its control button."""
    document = _declaration()
    assert set(document) == {"apiVersion", "kind", "metadata", "spec"}
    assert set(document["spec"]) == {"capabilities", "ports"}
    assert set(document["spec"]["capabilities"]) == {"core.lifecycle", "core.health"}
