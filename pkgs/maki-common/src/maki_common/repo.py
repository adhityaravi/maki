"""Shared git repository initialization — clone or pull, plus a multi-repo workspace registry."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


# Per-invocation subprocess deadlines for every `git` process spawned via
# ``_run_git`` / ``_run_git_no_cwd``. Without these, a hung git subprocess
# (DNS stall, SSH interactive prompt on a misconfigured URL, TLS handshake
# with a stuck GitHub edge, ``.git/index.lock`` contention with another
# process, paused fetch on a giant pack) pins the awaiting coroutine
# forever. Blast radius: ``hard_sync`` at the top of every cortex
# reasoning turn, ``init_repo`` at pod startup, and every
# ``RepoRegistry.resolve`` for an unknown-yet repo — one hung git wedges
# the entire tool-call pipeline while heartbeats (a separate task) still
# report healthy. ``claude.py``'s ``DEFAULT_INVOKE_TIMEOUT_S`` guards the
# same failure mode for the Claude CLI (see #350); the same reasoning
# applies here, even more so because git hangs on more common failure
# modes than the Claude CLI. See #499.
GIT_LOCAL_TIMEOUT_S = 120.0
"""Deadline for local-only git ops (``reset``, ``clean``, ``config``, ``rev-parse``, ...).

No legitimate local operation should ever run this long — the only paths
that get there involve pathological repo state or a stuck lock, both of
which we'd rather fail loudly for than wait on."""

GIT_NETWORK_TIMEOUT_S = 300.0
"""Deadline for network git ops (``fetch``, ``pull``, ``push``, ``clone``, ``ls-remote``).

Sized to accommodate slow-but-legitimate cases: a fresh clone of a
several-hundred-MB pack over a mediocre link, or a push of a large
delta. Well below any reasonable "call is now stuck" threshold."""

# git subcommands that touch the network. Auto-detected from ``args[0]``
# so callers don't need to plumb a timeout kwarg through every call site
# — the set is small and stable, and if it drifts, the only cost is the
# more generous deadline gets used for a local op (still fails loudly).
_NETWORK_GIT_SUBCOMMANDS = frozenset({"fetch", "pull", "push", "clone", "ls-remote"})


def _git_op_timeout(args: tuple[str, ...] | list[str]) -> float:
    """Pick a subprocess deadline based on the git subcommand in *args*.

    Skips leading option flags (``-C <path>``, ``-c <key>=<value>``) and
    treats the first bare positional as the subcommand. Network ops
    (:data:`_NETWORK_GIT_SUBCOMMANDS`) get :data:`GIT_NETWORK_TIMEOUT_S`;
    everything else gets :data:`GIT_LOCAL_TIMEOUT_S`.
    """
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == "-C" or arg == "-c":
            # These flags take a value in the NEXT arg.
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        return GIT_NETWORK_TIMEOUT_S if arg in _NETWORK_GIT_SUBCOMMANDS else GIT_LOCAL_TIMEOUT_S
    return GIT_LOCAL_TIMEOUT_S


async def _communicate_with_timeout(
    proc: asyncio.subprocess.Process,
    timeout: float,
    cmd_desc: str,
) -> tuple[bytes, bytes]:
    """Await ``proc.communicate()`` under a deadline, killing the process on expiry.

    A zombie git process still holds ``.git/index.lock``; leaving one
    behind would wedge every subsequent git op on the same repo. Try
    ``terminate()`` first (SIGTERM), fall back to ``kill()`` (SIGKILL)
    if the process refuses to exit within a short grace window.

    On expiry raises the builtin :class:`TimeoutError` (which is the same
    object as ``asyncio.TimeoutError`` on Python 3.11+); *cmd_desc* is
    embedded in the error message and the log record so operators can
    tell which git call died.
    """
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        # Kill the runaway git before it holds .git/index.lock indefinitely.
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=5.0)
        log.error(
            "git subprocess timed out",
            extra={"cmd": cmd_desc, "timeout_s": timeout},
        )
        raise TimeoutError(f"git {cmd_desc} exceeded {timeout}s") from None


# GitHub installation tokens leak via git's own error messages, e.g.
#   fatal: unable to access 'https://x-access-token:ghs_xxxxx@github.com/...': 403
# Strip the secret before anything reaches a log aggregator or an MCP result.
_TOKEN_URL_RE = re.compile(r"x-access-token:[^@\s/]+@")
# Raw GitHub token prefixes — installation (ghs_), personal (ghp_), user (gho_),
# refresh (ghr_), server-to-server (ghu_). Belt-and-suspenders in case the token
# ever shows up outside the URL form.
_BARE_TOKEN_RE = re.compile(r"\bgh[suporf]_[A-Za-z0-9]{20,}")
# `Authorization: Basic <base64>` — the form we now inject via
# `git -c http.extraheader=...` (issue #347). git itself should never echo
# the header value back, but redact defensively if it ever surfaces in a
# config dump, a process listing copy-pasted into a log, etc.
_BASIC_AUTH_RE = re.compile(r"(Authorization:\s*Basic\s+)[A-Za-z0-9+/=]+", re.IGNORECASE)


def redact_token(text: str) -> str:
    """Strip GitHub installation tokens from arbitrary text.

    Handles the `x-access-token:TOKEN@host` URL form (the form older versions
    embedded directly in the remote URL — see issue #347), the bare `ghs_*`
    / `ghp_*` / `gho_*` / `ghu_*` / `ghr_*` token prefixes as a defensive
    fallback, and the `Authorization: Basic <base64>` HTTP-header form we
    now inject per-invocation via `git -c http.extraheader=...`. Safe to
    call on stdout, stderr, log strings, MCP results — any caller-visible
    output that may have come from a git process.
    """
    if not text:
        return text
    text = _TOKEN_URL_RE.sub("x-access-token:***@", text)
    text = _BARE_TOKEN_RE.sub("***", text)
    text = _BASIC_AUTH_RE.sub(r"\1***", text)
    return text


def _auth_config_args(token: str) -> tuple[str, ...]:
    """Return ``git -c http.extraheader=...`` flags that auth a single invocation.

    The installation token lives only as a substring of the spawned git
    process's argv — there is no ``git remote set-url`` and therefore no
    token persisted to ``<repo>/.git/config`` (issue #347). The encoded
    header is ``Authorization: Basic <base64(x-access-token:TOKEN)>``, the
    standard GitHub installation-token HTTP auth form.

    Argv-only exposure is materially tighter than the previous
    disk-persistence pattern: under the old design any sibling process
    that could read ``.git/config`` (diagnostic dumps, side-car backups,
    a stray ``cat`` from another tool) saw the token in cleartext for the
    full ~1h token TTL. Here the token exists only for the git process's
    own lifetime.
    """
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ("-c", f"http.extraheader=Authorization: Basic {encoded}")


async def _run_git(
    repo_path: str,
    *args: str,
    token: str | None = None,
    timeout: float | None = None,
) -> tuple[int, str, str]:
    """Run a git command and return (returncode, stdout, stderr).

    When *token* is supplied, the GitHub installation token is injected via
    ``git -c http.extraheader=...`` so it authenticates the single
    invocation without ever landing in ``<repo>/.git/config`` (issue #347).
    Local-only operations (``reset``, ``clean``, ``config``, ``rev-parse``)
    don't need a token; remote operations (``fetch``, ``pull``, ``push``,
    ``clone``) do.

    stdout and stderr are pre-redacted — any embedded GitHub token (in URLs
    git echoes back in errors, or otherwise) is stripped before the bytes
    leave this function. Every caller — logger, MCP tool, exception path —
    therefore sees safe text by construction.

    A per-invocation deadline (``timeout``) bounds the wait for the
    subprocess: pass an explicit float to override, or leave ``None`` to
    auto-pick via :func:`_git_op_timeout` (network subcommands get
    :data:`GIT_NETWORK_TIMEOUT_S`, everything else
    :data:`GIT_LOCAL_TIMEOUT_S`). On expiry the git process is
    terminated (SIGTERM, then SIGKILL) so a zombie doesn't hold
    ``.git/index.lock``, and :class:`TimeoutError` propagates — see
    :func:`_communicate_with_timeout` and issue #499.
    """
    cmd_args: list[str] = []
    if token:
        cmd_args.extend(_auth_config_args(token))
    cmd_args.extend(["-C", repo_path, *args])
    effective_timeout = timeout if timeout is not None else _git_op_timeout(args)
    proc = await asyncio.create_subprocess_exec(
        "git",
        *cmd_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await _communicate_with_timeout(proc, effective_timeout, " ".join(args))
    return proc.returncode, redact_token(stdout.decode()), redact_token(stderr.decode())


def clean_remote_url(owner: str, name: str) -> str:
    """Return the token-free HTTPS clone URL for a GitHub repo.

    Single source of truth for the remote URL form. Change here to add SSH
    support, GHE hosts, or a different naming scheme — every clone/set-url
    call flows through this helper, so drift between call sites is impossible.

    Tokens are NEVER embedded in the URL (issue #347); they inject
    per-invocation via ``_run_git(..., token=...)`` which sets an in-memory
    ``http.extraheader`` config. The URL that lands in ``.git/config`` is
    always the plain ``https://github.com/owner/name.git`` form.
    """
    return f"https://github.com/{owner}/{name}.git"


async def set_origin(repo_path: str, url: str) -> tuple[int, str]:
    """Point ``origin`` at *url* via ``git remote set-url``.

    Consolidates the fetch/push/pull setup pattern used by ``hard_sync``,
    ``init_repo`` and the MCP git tools (``git_commit_and_push``,
    ``git_pull``). Returns ``(returncode, stderr)`` so callers pick their
    own failure semantics — raise ``SyncError``, log-and-continue, etc.
    ``stderr`` is pre-redacted by ``_run_git``.

    Pair with ``clean_remote_url(owner, name)`` when constructing the URL
    from an owner/name; pass an already-known URL through directly (as
    ``init_repo`` does with its ``clone_url`` argument).
    """
    rc, _, stderr = await _run_git(repo_path, "remote", "set-url", "origin", url)
    return rc, stderr


async def _run_git_no_cwd(
    *args: str,
    token: str | None = None,
    timeout: float | None = None,
) -> tuple[int, str, str]:
    """Run a git command without a ``-C`` working directory (e.g. ``clone``).

    Same per-invocation auth injection, redaction, and subprocess-timeout
    guarantees as :func:`_run_git`. See #499 for the timeout rationale.
    """
    cmd_args: list[str] = []
    if token:
        cmd_args.extend(_auth_config_args(token))
    cmd_args.extend(args)
    effective_timeout = timeout if timeout is not None else _git_op_timeout(args)
    proc = await asyncio.create_subprocess_exec(
        "git",
        *cmd_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await _communicate_with_timeout(proc, effective_timeout, " ".join(args))
    return proc.returncode, redact_token(stdout.decode()), redact_token(stderr.decode())


class SyncError(Exception):
    """Raised when ``hard_sync`` cannot reset the working tree to ``origin/main``.

    Callers MUST treat the working tree as stale on this exception — the
    previous inline pipeline in cortex (`fetch` → `reset --hard` → `clean`)
    only logged a warning on the failing step and silently continued, so a
    transient ``fetch`` failure would still run ``reset --hard origin/main``
    against whatever was last fetched (potentially hours or days stale). See
    issue #290.

    The ``stderr`` field is pre-redacted via ``_run_git`` and therefore safe
    to forward into logs and NATS payloads.
    """

    def __init__(self, step: str, returncode: int | None, stderr: str) -> None:
        self.step = step
        self.returncode = returncode
        self.stderr = stderr
        snippet = (stderr or "").strip().splitlines()
        head = snippet[0] if snippet else ""
        super().__init__(f"git {step} failed (rc={returncode}): {head[:200]}")


async def hard_sync(
    repo_path: str,
    *,
    github_auth: Any | None = None,
    owner: str | None = None,
    name: str | None = None,
    clone_url: str | None = None,
) -> None:
    """Hard-reset *repo_path* to ``origin/main`` with abort-on-first-failure semantics.

    Pipeline (each step aborts on non-zero returncode by raising ``SyncError``):

      1. optional ``git remote set-url origin <CLEAN_URL>`` — only when
         *github_auth* is supplied. The URL is **token-free** by design
         (issue #347): we mint the token in memory but rewrite the remote
         to the plain ``https://github.com/...`` form, both establishing
         the no-token-on-disk invariant and scrubbing any legacy
         token-embedded URL left behind by older versions. Requires either
         *clone_url* or both *owner* and *name* so the URL can be
         constructed.
      2. ``git -c http.extraheader=... fetch origin main`` — the
         installation token authenticates this single invocation via an
         in-memory ``-c`` config; it never lands in ``.git/config``.
      3. ``git reset --hard origin/main`` (local; no auth needed)
      4. ``git clean -fd`` (local; no auth needed)

    This replaces the inline pipeline in ``maki_cortex._process_turn`` whose
    bug — log-and-continue on failure — let cortex reason about stale code
    with only one buried warning line as a signal. By raising on the first
    non-zero returncode this function makes the silent-stale-code path
    impossible: the caller either succeeds in landing on the latest
    ``origin/main`` or sees a clear failure.

    All stderr surfaced via ``SyncError.stderr`` is pre-redacted by
    ``_run_git`` (no installation tokens leak).
    """
    token: str | None = None
    if github_auth is not None:
        if not clone_url and not (owner and name):
            raise ValueError(
                "hard_sync: github_auth requires either clone_url or (owner, name) to construct the auth URL"
            )
        try:
            token = await github_auth.get_token()
        except Exception as exc:  # noqa: BLE001 — surface as a typed sync failure
            raise SyncError("token", None, f"failed to mint installation token: {exc}") from exc
        # Rewrite the remote to the **token-free** URL. This both enforces
        # the no-token-on-disk invariant for the fetch step below (which
        # auths via -c http.extraheader instead) and scrubs any legacy
        # `x-access-token:...@github.com` URL written by older versions of
        # this module — see issue #347.
        clean_url = clone_url if clone_url else clean_remote_url(owner, name)
        try:
            rc, stderr = await set_origin(repo_path, clean_url)
        except TimeoutError as exc:
            raise SyncError("remote set-url", None, str(exc)) from exc
        if rc != 0:
            raise SyncError("remote set-url", rc, stderr)

    # A subprocess timeout inside any step is functionally a sync failure
    # — we haven't landed on ``origin/main`` — so map ``TimeoutError`` onto
    # ``SyncError`` (rc=None to signal "no exit code available") and let
    # callers keep their single-exception contract. Without this, cortex's
    # ``_process_turn`` (which only catches ``SyncError``) would see an
    # unhandled ``TimeoutError`` and either wedge the turn task or bubble
    # up to a supervisor. See #499.
    try:
        fetch_rc, _, fetch_err = await _run_git(repo_path, "fetch", "origin", "main", token=token)
    except TimeoutError as exc:
        raise SyncError("fetch", None, str(exc)) from exc
    if fetch_rc != 0:
        raise SyncError("fetch", fetch_rc, fetch_err)

    for step, args in (
        ("reset", ("reset", "--hard", "origin/main")),
        ("clean", ("clean", "-fd")),
    ):
        try:
            rc, _, stderr = await _run_git(repo_path, *args)
        except TimeoutError as exc:
            raise SyncError(step, None, str(exc)) from exc
        if rc != 0:
            raise SyncError(step, rc, stderr)


def build_github_auth(
    app_id: str | None,
    private_key: str | None,
    installation_id: str | None,
) -> Any | None:
    """Construct a ``GitHubAuth`` if all three config fields are present, else ``None``.

    A tiny factory so callers (cortex startup, immune startup) don't each
    import ``maki_common.tools.github.GitHubAuth`` and repeat the
    "all-three-or-nothing" guard. Keeps the ``GitHubAuth`` import in one
    place and lets ``init_repo`` / ``hard_sync`` remain
    ``github_auth``-shaped without leaking the concrete class to every
    component.
    """
    if not (app_id and private_key and installation_id):
        return None
    from maki_common.tools.github import GitHubAuth

    return GitHubAuth(app_id, private_key, installation_id)


async def init_repo(
    repo_path: str,
    clone_url: str,
    github_auth: Any | None = None,
    git_user: str = "makiself[bot]",
    git_email: str = "makiself[bot]@users.noreply.github.com",
) -> bool:
    """Clone or pull a git repo. Returns True on success.

    Authentication (issue #347): when *github_auth* is provided we mint a
    short-lived installation token and inject it per-invocation via
    ``git -c http.extraheader=...`` (see ``_auth_config_args``). The token
    is **never** embedded in the remote URL and therefore **never** lands
    in ``<repo>/.git/config``. The on-disk URL is always the plain
    ``https://github.com/owner/repo.git`` form.

    Args:
        repo_path: Local path for the clone.
        clone_url: HTTPS clone URL (e.g. https://github.com/owner/repo.git).
            Must be token-free; the token (if any) is added per-invocation.
        github_auth: GitHubAuth instance — if provided, mints a token and
            authenticates clone/pull via an in-memory header.
        git_user: Git user.name for commits.
        git_email: Git user.email for commits.
    """
    token: str | None = None
    if github_auth:
        try:
            token = await github_auth.get_token()
        except Exception:
            log.warning("Failed to get GitHub token, cloning without auth")

    # Subprocess timeouts (see #499) surface as ``TimeoutError`` from
    # ``_run_git`` / ``_run_git_no_cwd``. ``init_repo`` returns ``bool`` and
    # its callers (cortex/immune startup, ``RepoRegistry.resolve`` under
    # ``clone_lock``) already treat ``False`` as "clone/pull didn't
    # succeed" — mapping a hung git onto that same signal preserves the
    # existing contract without letting the exception escape and pin the
    # calling coroutine forever.
    if not os.path.exists(os.path.join(repo_path, ".git")):
        log.info("Cloning repo", extra={"repo_path": repo_path})
        parent = os.path.dirname(repo_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            rc, _, stderr = await _run_git_no_cwd("clone", clone_url, repo_path, token=token)
        except TimeoutError as exc:
            log.error("Git clone timed out", extra={"repo_path": repo_path, "error": str(exc)})
            return False
        if rc != 0:
            log.error("Git clone failed", extra={"stderr": stderr})
            return False
        log.info("Repo cloned", extra={"repo_path": repo_path})

        # Configure git identity (local ops — still time-bounded so a stuck
        # `.git/index.lock` from a sibling process can't wedge startup).
        try:
            await _run_git(repo_path, "config", "user.name", git_user)
            await _run_git(repo_path, "config", "user.email", git_email)
        except TimeoutError as exc:
            log.warning("Git identity config timed out", extra={"repo_path": repo_path, "error": str(exc)})
            # Fall through — the clone succeeded; a missing local identity
            # only bites at the first commit, and the caller learns via a
            # loud commit-time failure instead of a silent startup wedge.
        return True
    else:
        log.info("Pulling latest", extra={"repo_path": repo_path})
        if github_auth:
            # Rewrite origin to the token-free URL. This both establishes
            # the no-token-on-disk invariant for the pull below (which auths
            # via -c http.extraheader) and scrubs any legacy
            # `x-access-token:...@github.com` URL written by older versions
            # of this module — see issue #347.
            try:
                await set_origin(repo_path, clone_url)
            except TimeoutError as exc:
                log.warning("Git remote set-url timed out", extra={"repo_path": repo_path, "error": str(exc)})
                return False
        try:
            rc, _, stderr = await _run_git(repo_path, "pull", "--rebase", "origin", "main", token=token)
        except TimeoutError as exc:
            log.warning("Git pull timed out", extra={"repo_path": repo_path, "error": str(exc)})
            return False
        if rc != 0:
            log.warning("Git pull failed", extra={"stderr": stderr})
            return False
        log.info("Repo updated", extra={"repo_path": repo_path})
        return True


# ---------------------------------------------------------------------------
# Multi-repo workspace registry
# ---------------------------------------------------------------------------


@dataclass
class RepoEntry:
    """One known git repo workspace.

    `path` is the local clone directory. `owner`/`name` identify the GitHub
    repo for push-URL construction. `auth` is the GitHubAuth used for clone
    and push; if None, operations run without injected credentials.

    `last_synced_at` is a monotonic-clock timestamp (see ``time.monotonic``)
    of the last successful ``hard_sync`` on this entry. ``RepoRegistry.resolve``
    uses it to gate per-resolve auto-sync behind a TTL, so a burst of tool
    calls hitting the same auxiliary repo folds down to one network fetch. The
    initial ``0.0`` is intentional — it forces the first ``resolve`` after a
    process restart (or after a clone that pre-dates the sync mechanism) to
    fetch fresh, rather than trusting whatever snapshot happens to be on disk.
    """

    path: str
    owner: str
    name: str
    auth: Any | None = None
    clone_url: str | None = None
    last_synced_at: float = 0.0

    def resolved_clone_url(self) -> str:
        return self.clone_url or clean_remote_url(self.owner, self.name)


class RepoRegistry:
    """Resolve a `repo` MCP-tool argument to a local workspace, cloning on demand.

    The cortex/immune service registers its primary repo (maki) as the default
    at startup. Tools that accept a `repo` arg call `await registry.resolve(arg)`:

        - `arg` empty/None  → default repo (no-arg behaviour preserved)
        - `arg = "name"`    → already-registered repo with that short name
        - `arg = "owner/name"` → fully-qualified; auto-registered + cloned at
          `/repo/<name>` (or `workspace_root/<name>`) using the default repo's
          GitHubAuth credentials

    Auto-clones inherit the default repo's auth — typically the makiself[bot]
    installation, which has access to every repo in adhityaravi/*. A repo the
    bot has no installation access to will fail at clone with a redacted error.

    Freshness (issue #450). Every ``resolve`` also fast-forwards the workspace
    to ``origin/main`` via ``hard_sync``, bounded by ``sync_ttl_seconds`` per
    repo — so a burst of tool calls in the same turn folds down to one fetch,
    and repos-that-haven't-been-touched-in-days still get fresh code on the
    next resolve. Without this, the primary maki repo stayed fresh (cortex
    calls ``hard_sync`` at the top of each turn) but auxiliary repos
    auto-registered via ``owner/name`` were cloned once and never pulled again
    — every subsequent read hit whatever snapshot happened to be on disk. Same
    silent-stale-code failure mode ``hard_sync`` was invented to prevent
    (#290), on a different code path. On sync failure the resolver returns
    ``None`` rather than silently serving the stale on-disk snapshot,
    mirroring the "loud failure over quiet staleness" contract cortex already
    holds. Pass ``sync_ttl_seconds=float("inf")`` to disable auto-sync (used
    by tests with fake ``.git`` fixtures that would trip the real fetch).
    """

    def __init__(
        self,
        workspace_root: str = "/repo",
        *,
        sync_ttl_seconds: float = 60.0,
    ) -> None:
        self._repos: dict[str, RepoEntry] = {}
        self._default_key: str | None = None
        self._workspace_root = workspace_root
        self._clone_lock = asyncio.Lock()
        # Per-repo sync lock so a burst of concurrent tool calls against the
        # same repo collapses to one fetch (the losers see fresh state under
        # the re-check inside the lock) — but concurrent resolves against
        # *different* repos don't serialize on each other.
        self._sync_locks: dict[str, asyncio.Lock] = {}
        self._sync_ttl_seconds = sync_ttl_seconds

    def register(
        self,
        entry: RepoEntry,
        *,
        default: bool = False,
        aliases: tuple[str, ...] = (),
    ) -> None:
        """Register a repo. Keyed by both short name and `owner/name`."""
        full_key = f"{entry.owner}/{entry.name}"
        self._repos[full_key] = entry
        self._repos[entry.name] = entry
        for alias in aliases:
            self._repos[alias] = entry
        if default or self._default_key is None:
            self._default_key = full_key

    def default(self) -> RepoEntry | None:
        if self._default_key is None:
            return None
        return self._repos.get(self._default_key)

    def known(self) -> list[str]:
        """Return the list of registered keys (for diagnostics/error messages)."""
        return sorted(set(self._repos.keys()))

    def _sync_lock_for(self, entry: RepoEntry) -> asyncio.Lock:
        """Return (creating if needed) the async lock for this repo's sync path."""
        key = f"{entry.owner}/{entry.name}"
        lock = self._sync_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._sync_locks[key] = lock
        return lock

    async def resolve(self, repo_key: str | None) -> RepoEntry | None:
        """Resolve a repo arg to an entry, cloning unknown `owner/name` on demand.

        After ensuring the clone exists, fast-forward to ``origin/main`` via
        ``hard_sync`` when the entry's last-synced timestamp is older than
        ``sync_ttl_seconds``. See the class docstring for the rationale
        (issue #450) — the short version: without this, auxiliary repos went
        stale forever.

        Returns None when:
          - the registry has no default and ``repo_key`` is empty;
          - an unknown key cannot be auto-registered (no ``owner/name`` form);
          - the clone attempt failed;
          - the sync attempt failed (we refuse to serve a known-possibly-stale
            snapshot; loud failure over silent staleness — issue #290).
        """
        if not repo_key:
            entry = self.default()
            if entry is None:
                return None
        elif repo_key in self._repos:
            entry = self._repos[repo_key]
        elif "/" in repo_key:
            owner, name = repo_key.split("/", 1)
            owner = owner.strip()
            name = name.strip()
            if not owner or not name:
                return None
            default_entry = self.default()
            entry = RepoEntry(
                path=os.path.join(self._workspace_root, name),
                owner=owner,
                name=name,
                auth=default_entry.auth if default_entry else None,
            )
            self.register(entry)
        else:
            return None

        # Ensure the clone exists. Serialize so two concurrent tool calls
        # against a fresh repo don't race the clone.
        if not os.path.exists(os.path.join(entry.path, ".git")):
            async with self._clone_lock:
                if not os.path.exists(os.path.join(entry.path, ".git")):
                    ok = await init_repo(entry.path, entry.resolved_clone_url(), github_auth=entry.auth)
                    if not ok:
                        return None
                    # A fresh clone lands on origin/HEAD by definition — treat
                    # it as just-synced so we don't immediately fetch again.
                    entry.last_synced_at = time.monotonic()
                    return entry

        # Fast-forward to origin/main if the last sync is older than the TTL.
        # The TTL bounds the network cost of a burst of tool calls against the
        # same repo: only the first crosses the threshold; the rest see fresh
        # state. `float("inf")` disables auto-sync entirely (used by tests
        # that seed a fake `.git` dir which isn't a real git workspace).
        if time.monotonic() - entry.last_synced_at < self._sync_ttl_seconds:
            return entry

        async with self._sync_lock_for(entry):
            # Re-check under the lock — a concurrent resolve may have just
            # completed a sync while we waited.
            if time.monotonic() - entry.last_synced_at < self._sync_ttl_seconds:
                return entry
            try:
                await hard_sync(
                    entry.path,
                    github_auth=entry.auth,
                    clone_url=entry.resolved_clone_url(),
                )
            except SyncError as exc:
                # Refuse to serve possibly-stale code. The alternative
                # (log-and-return-entry) is exactly the silent-stale-code
                # failure mode issue #290 was written to eliminate.
                # `exc.stderr` is pre-redacted by `_run_git`.
                log.warning(
                    "Auto-sync failed for %s/%s — refusing to serve stale disk state",
                    entry.owner,
                    entry.name,
                    extra={
                        "step": exc.step,
                        "returncode": exc.returncode,
                        "stderr": exc.stderr,
                    },
                )
                return None
            entry.last_synced_at = time.monotonic()

        return entry
