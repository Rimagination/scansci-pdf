"""The publisher stack must have one implementation, not two (#71 item 5).

The repository shipped two copies of the publisher stack: one at the package
root and one under ``scansci_pdf.institutional/``. They were both live --
``main.py`` and ``sources/__init__.py`` used the institutional one, the CLI
and ``acs_batch.py`` used the root one -- and they had already drifted: the
publisher-PDF-capture work from #69 landed in the root copy only, and the
Optica profile from #59 landed in the institutional copy only.

The copies are now one implementation plus re-export shims. These tests pin
that, because the failure mode is a silent one: a fix applied to one copy
looks fine until the other copy runs it.

The Optica profile is the clearest case of the drift. The *root* registry
had no Optica entry, and the root registry is what ``_cli_core.py`` and
``acs_batch.py`` use. The shipped ``scansci-pdf`` console script goes through
``main.py``, which already used the institutional registry and therefore
already had Optica -- so the gap was in the root registry, not in the CLI.
"""

from __future__ import annotations

import ast
import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# (institutional module, canonical module at the package root)
PAIRS = [
    ("scansci_pdf.institutional.publisher_batch", "scansci_pdf.publisher_batch"),
    ("scansci_pdf.institutional.publisher_profiles", "scansci_pdf.publisher_profiles"),
    ("scansci_pdf.institutional.publisher_access", "scansci_pdf.publisher_access"),
    ("scansci_pdf.institutional.publisher_pdf_router", "scansci_pdf.publisher_pdf_router"),
    ("scansci_pdf.institutional.http_utils", "scansci_pdf.http_utils"),
    ("scansci_pdf.institutional.session_store", "scansci_pdf.session_store"),
    ("scansci_pdf.institutional.session_broker", "scansci_pdf.session_broker"),
    ("scansci_pdf.institutional.flaresolverr", "scansci_pdf.flaresolverr"),
    ("scansci_pdf.institutional.cloakbrowser_compat", "scansci_pdf.cloakbrowser_compat"),
    (
        "scansci_pdf.institutional.extractors.html_extractor",
        "scansci_pdf.extractors.html_extractor",
    ),
    (
        "scansci_pdf.institutional.extractors.pdf_extractor",
        "scansci_pdf.extractors.pdf_extractor",
    ),
]
for _adapter in ["acs", "elsevier", "generic", "nature", "rsc", "tandfonline", "wiley"]:
    PAIRS.append((
        f"scansci_pdf.institutional.extractors.publisher_adapters.{_adapter}",
        f"scansci_pdf.extractors.publisher_adapters.{_adapter}",
    ))

IDS = [shim.rsplit(".", 1)[-1] if "publisher_adapters" in shim else shim for shim, _ in PAIRS]


def _public_names(module) -> list[str]:
    """The canonical module's public names, skipping the ``None`` placeholders."""
    names = []
    for name in dir(module):
        if name.startswith("__"):
            continue
        if getattr(module, name, None) is None:
            continue
        names.append(name)
    return names


@pytest.mark.parametrize("shim_name,canon_name", PAIRS, ids=IDS)
def test_the_old_import_path_gives_the_canonical_objects(shim_name, canon_name):
    """Both import paths must hand back the *same* objects, not equal copies."""
    shim = importlib.import_module(shim_name)
    canon = importlib.import_module(canon_name)

    for name in _public_names(canon):
        value = getattr(canon, name)
        assert getattr(shim, name) is value, (
            f"{shim_name}.{name} is not the same object as {canon_name}.{name}"
        )


@pytest.mark.parametrize("shim_name,canon_name", PAIRS, ids=IDS)
def test_a_canonical_reload_does_not_leave_the_shim_stale(shim_name, canon_name):
    """A reload of the canonical module must stay visible through the shim.

    The shims bind nothing at import time, so reloading the canonical module
    replaces the objects it holds and the old import path must follow. A shim
    that captured the names eagerly would keep handing back the pre-reload
    objects here.
    """
    shim = importlib.import_module(shim_name)
    canon = importlib.import_module(canon_name)

    saved = dict(canon.__dict__)
    try:
        importlib.reload(canon)
        for name in _public_names(canon):
            value = getattr(canon, name)
            assert getattr(shim, name) is value, (
                f"{shim_name}.{name} went stale after reloading {canon_name}"
            )
    finally:
        # Put the original namespace back: other modules in this session may
        # hold references to the objects the reload replaced.
        canon.__dict__.clear()
        canon.__dict__.update(saved)


@pytest.mark.parametrize("shim_name,canon_name", PAIRS, ids=IDS)
def test_the_institutional_module_holds_no_implementation_of_its_own(shim_name, canon_name):
    """A second copy can only come back if someone writes one.

    This is the check that would have caught the drift: a new function or
    class defined in the shim means the two paths have started to diverge
    again, and every fix now has to be written twice. The walk covers the
    whole tree, so a definition hidden inside an ``if`` block counts too.
    """
    shim = importlib.import_module(shim_name)
    source = Path(shim.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    own = sorted({
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    })
    assert own == ["__dir__", "__getattr__"], (
        f"{shim_name} defines {own} -- the implementation belongs in {canon_name}"
    )

    lambdas = [node for node in ast.walk(tree) if isinstance(node, ast.Lambda)]
    assert not lambdas, f"{shim_name} defines a lambda -- that is an implementation too"

    # Nothing but the shim machinery may live in the module namespace: a
    # module-level constant or helper is exactly how a second copy starts.
    extra = {
        name
        for name in shim.__dict__
        if not (name.startswith("__") and name.endswith("__"))
    } - {"_canonical", "__getattr__", "__dir__"}
    assert extra == set(), f"{shim_name} defines extra names: {sorted(extra)}"


def test_star_import_from_a_shim_still_exports_the_canonical_names():
    """The old duplicate modules were star-imported; that must keep working."""
    namespace: dict = {}
    exec("from scansci_pdf.institutional.http_utils import *", namespace)
    for name in ("request_with_retry", "logger", "requests", "time", "logging"):
        assert name in namespace, f"star import dropped {name}"


def test_the_optica_profile_reaches_both_paths():
    """#59's profile was only in the institutional copy, so the root registry
    (the one ``_cli_core.py`` and ``acs_batch.py`` use) never saw it."""
    from scansci_pdf.institutional import publisher_profiles as institutional
    from scansci_pdf import publisher_profiles as canonical

    for module in (canonical, institutional):
        profile = module.get_publisher_profile("optica")
        assert profile.name == "Optica"
        assert module.infer_publisher_profile("10.1364/OL.47.013297") is profile
        assert "optica" in module.list_publisher_profiles()

    assert institutional.get_publisher_profile("optica") is canonical.get_publisher_profile(
        "optica"
    )


@pytest.mark.parametrize(
    "alias",
    ["optica", "osa", "opg", "optica-publishing-group", "optical-society-of-america", "Optica"],
)
def test_the_optica_profile_resolves_by_every_alias(alias):
    """By key, by alias and by display name -- on both import paths."""
    from scansci_pdf.institutional import publisher_profiles as institutional
    from scansci_pdf import publisher_profiles as canonical

    for module in (canonical, institutional):
        profile = module.get_publisher_profile("optica")
        assert module.get_publisher_profile(alias) is profile


def test_an_entitled_tsinghua_acs_page_is_not_read_as_logged_out():
    """The ACS title markers must not swallow the institutional session.

    An entitled Tsinghua article page carries "Tsinghua" in its <title>; the
    title check ran before the body check, so the page was classified as
    logged out even though the body says the institution has access.
    """
    from scansci_pdf.publisher_batch import PublisherBatchDownloader
    from scansci_pdf.publisher_profiles import ACS_PROFILE

    class _FakePage:
        url = "https://pubs.acs.org/doi/10.1021/acs.est.6c00693"

        def title(self):
            return "Brought to you by Tsinghua University - pubs.acs.org"

        def locator(self, _selector):
            class _Body:
                def inner_text(self, timeout=None):
                    return "Brought to you by Tsinghua University China. Institutional access."

            return _Body()

    downloader = PublisherBatchDownloader({}, profile=ACS_PROFILE)
    assert downloader._looks_logged_out(_FakePage()) is False


def test_a_title_marker_does_not_match_inside_a_longer_word():
    """Title markers are matched on word boundaries, not as bare substrings.

    "Sign In" also occurs inside "deSIGN IN", and titles such as "Catalyst
    Design in a Single Step" belong to ordinary entitled article pages.
    Reading those as logged out aborts a download the institution can do.
    """
    from scansci_pdf.publisher_batch import PublisherBatchDownloader
    from scansci_pdf.publisher_profiles import ACS_PROFILE

    class _FakePage:
        url = "https://pubs.acs.org/doi/10.1021/acs.est.6c00693"

        def __init__(self, title: str):
            self._title = title

        def title(self):
            return self._title

        def locator(self, _selector):
            class _Body:
                def inner_text(self, timeout=None):
                    return "Brought to you by Tsinghua University China. Institutional access."

            return _Body()

    downloader = PublisherBatchDownloader({}, profile=ACS_PROFILE)
    for title in (
        "Catalyst Design in a Single Step - pubs.acs.org",
        "How to Resign in a New Light - pubs.acs.org",
        "Assign in Practice - pubs.acs.org",
    ):
        assert downloader._looks_logged_out(_FakePage(title)) is False, title
    # The genuine logged-out title still has to be detected.
    assert downloader._looks_logged_out(_FakePage("Sign In - pubs.acs.org")) is True


def test_an_explicitly_empty_config_is_not_replaced_by_the_on_disk_config(monkeypatch):
    """``{}`` means "no config", not "no config given"."""
    from scansci_pdf import publisher_batch

    monkeypatch.setattr(publisher_batch, "load_config", lambda: {"browser_headless": True})
    assert publisher_batch.PublisherBatchDownloader({}).config == {}


def test_the_interactive_downloader_never_launches_a_headless_browser(tmp_path, monkeypatch):
    """The institutional path drives a manual SSO/2FA login.

    Its downloader was always visible; after the convergence the shared
    implementation honours ``browser_headless`` instead, which would make
    that login impossible. The flag puts the old behaviour back.
    """
    from scansci_pdf import browser_engine, publisher_batch

    seen: dict = {}

    def _fake_get_persistent_context(profile_dir, config=None):
        seen["config"] = config
        return object()

    monkeypatch.setattr(browser_engine, "get_persistent_context", _fake_get_persistent_context)

    interactive = publisher_batch.PublisherBatchDownloader(
        {"browser_headless": True},
        profile=publisher_batch.ACS_PROFILE,
        require_interactive_browser=True,
    )
    interactive._launch_context(profile_dir=tmp_path)
    assert seen["config"]["browser_headless"] is False

    plain = publisher_batch.PublisherBatchDownloader({"browser_headless": True})
    plain._launch_context(profile_dir=tmp_path)
    assert seen["config"]["browser_headless"] is True


def test_the_session_broker_keeps_the_old_default_base_dir_name():
    """The institutional session broker exposed ``DEFAULT_BASE_DIR``."""
    from scansci_pdf.institutional import session_broker as institutional
    from scansci_pdf import session_broker as canonical

    assert institutional.DEFAULT_BASE_DIR is canonical.DATA_DIR
    assert "DEFAULT_BASE_DIR" in institutional.__all__
    assert "DATA_DIR" in institutional.__all__


def test_the_broker_spawn_target_is_executable():
    """``start_broker_process`` spawns ``python -m scansci_pdf.cli``."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    proc = subprocess.run(
        [sys.executable, "-m", "scansci_pdf.cli", "session-broker-run", "--help"],
        capture_output=True,
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr[-2000:].decode("utf-8", "replace")
    assert "session-broker-run" in proc.stdout.decode("utf-8", "replace")


def test_the_broker_spawns_the_module_the_product_names(tmp_path):
    """The spawn target is read from the product, not copied into the test.

    The test above proves that *a* plausible command works. What has to hold
    is that the command the product builds works: a module-path typo in
    ``start_broker_process`` kills the whole broker path -- every spawn dies
    with "No module named ..." -- while every test stays green.
    """
    from unittest.mock import patch

    import scansci_pdf.session_broker as broker

    recorded: dict[str, list[str]] = {}

    class _FakePopen:
        def __init__(self, args, **kwargs):
            recorded["args"] = list(args)

        def poll(self):
            return None

    with patch.object(broker, "broker_dir", lambda publisher: tmp_path):
        with patch.object(broker.subprocess, "Popen", _FakePopen):
            broker.start_broker_process(
                publisher="elsevier",
                browser_profile="test-profile",
                institution="Test University",
                ttl_seconds=60,
                cwd=REPO,
            )

    args = recorded["args"]
    assert args[1:4] == ["-m", "scansci_pdf.cli", "session-broker-run"], args
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    proc = subprocess.run(
        args[:4] + ["--help"], capture_output=True, timeout=180, env=env
    )
    assert proc.returncode == 0, proc.stderr[-2000:].decode("utf-8", "replace")


@pytest.mark.parametrize("shim_name,canon_name", PAIRS, ids=IDS)
def test_a_shim_forwards_without_answering_a_name_of_its_own(shim_name, canon_name):
    """``__getattr__`` may only forward.

    A branch that answers one name itself is a second implementation,
    reachable only through the old import path, and the identity guard
    cannot see it: that guard compares the *canonical's* names, so a name
    the canonical does not have is never compared.
    """
    shim = importlib.import_module(shim_name)
    source = Path(shim.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "__getattr__"):
            continue
        answered = {
            comparator.value
            for compare in ast.walk(node)
            if isinstance(compare, ast.Compare)
            for comparator in compare.comparators
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str)
        }
        assert answered <= {"__all__"}, (
            f"{shim_name} answers names of its own: {sorted(answered)}"
        )


@pytest.mark.parametrize("shim_name,canon_name", PAIRS, ids=IDS)
def test_a_star_import_from_a_shim_exports_the_canonical_names(shim_name, canon_name):
    """``from ... import *`` must export the canonical module's public names.

    A module-level ``__all__`` in a shim would shadow the dynamic one, and
    a dunder is exempt from the namespace check -- so the export list has
    to be checked for every pair, not for one of them.
    """
    shim = importlib.import_module(shim_name)
    canon = importlib.import_module(canon_name)
    expected = {name for name in dir(canon) if not name.startswith("_")}
    assert set(shim.__all__) == expected, (
        f"{shim_name} exports {sorted(set(shim.__all__) ^ expected)} that the "
        f"canonical module does not"
    )
