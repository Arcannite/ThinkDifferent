#!/usr/bin/env python3
"""fstrackd - git-based filesystem journal for containerized benchmark trials.

Runs *inside* the task container, next to the agent. For every tracked root it
maintains an independent bare git repository whose work tree is that root. The
task's own ``.git`` is never touched: its history is what DeepSWE's verifier
uses to produce ``model.patch``.

Two triggers feed the journal:

* a polling scan of the work tree (default every 1s) catches changes made by
  any process, including compilers, tests and background jobs that never go
  through a tool call;
* ``events.jsonl`` (written by the pi extension) marks tool/turn boundaries.
  Each event forces a commit whose trailers identify the action, so a replay
  can map a conversation turn to an exact filesystem state.

Guardrails (the journal must never break the trial):

* ``FSTRACK_MAX_FILE_MB`` (default 8): files larger than this are not stored in
  git. ``FSTRACK_LARGE_MODE`` decides what happens instead:
    - ``manifest`` (default): write metadata to ``<slug>.large.jsonl``
    - ``skip``: ignore them entirely
    - ``full``: store them anyway (old behaviour; use deliberately)
* ``FSTRACK_MAX_REPO_MB`` (default 256): circuit breaker. If the current tree
  or the git repo would exceed this, content commits stop; empty turn markers
  and the large-file manifest continue. Set 0 to disable.
* non-regular files (FIFOs, sockets, devices) are never handed to git.
* a failed/timed-out git command clears a stale ``index.lock`` and backs off
  instead of retrying every scan.

Output lives under ``FSTRACK_OUT`` (default ``/logs/artifacts/fstrack``), which
the engine collects as an artifact:

    events.jsonl       event log (written by the harness extension)
    daemon.log         this process's stderr
    summary.json       policy, base/head commits, counts, repo size, over_cap
    index.jsonl        one line per commit (append-only)
    <slug>.large.jsonl large-file manifest (append-only)
    <slug>.git/        one bare repo per root
    <slug>.index       private index file (never shared with the task repo)
    <slug>.seq         per-root sequence counter
"""

from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(os.environ.get("FSTRACK_OUT", "/logs/artifacts/fstrack"))
ROOTS = [p for p in os.environ.get("FSTRACK_ROOTS", "/app").split(":") if p]
SCAN_INTERVAL = float(os.environ.get("FSTRACK_SCAN_INTERVAL", "1.0"))
POLL_INTERVAL = float(os.environ.get("FSTRACK_POLL_INTERVAL", "0.1"))
MAX_FILE_MB = float(os.environ.get("FSTRACK_MAX_FILE_MB", "8") or "8")
LARGE_MODE = (os.environ.get("FSTRACK_LARGE_MODE", "manifest") or "manifest").strip().lower()
MAX_REPO_MB = float(os.environ.get("FSTRACK_MAX_REPO_MB", "256") or "256")
HASH_LARGE = os.environ.get("FSTRACK_HASH_LARGE", "0").strip().lower() not in (
    "",
    "0",
    "false",
    "no",
)
GIT_TIMEOUT = int(os.environ.get("FSTRACK_GIT_TIMEOUT", "120") or "120")
MAX_BYTES = int(MAX_FILE_MB * 1024 * 1024) if MAX_FILE_MB > 0 else 0
MAX_REPO_BYTES = int(MAX_REPO_MB * 1024 * 1024) if MAX_REPO_MB > 0 else 0
if LARGE_MODE not in ("skip", "manifest", "full"):
    LARGE_MODE = "manifest"

# Patterns are matched against both the relative path and every path segment, so
# a bare name excludes that directory anywhere in the tree.
DEFAULT_EXCLUDES = [
    ".git",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    ".venv",
    "venv",
    "env",
    "target",
    "build",
    "dist",
    "coverage",
    ".next",
    ".nuxt",
    "*.pyc",
    "*.pyo",
    "*.o",
    "*.so",
    "*.a",
    "*.class",
    "*.rlib",
    "*.rmeta",
    "*.dylib",
    "*.dll",
    "*.exe",
    # Bulk media, disk images, archives and model weights. These are also
    # covered by max_file_mb when small, but excluding them avoids even the
    # metadata/hash path and makes intent explicit.
    "*.mp4",
    "*.mov",
    "*.mkv",
    "*.avi",
    "*.webm",
    "*.wmv",
    "*.flv",
    "*.wav",
    "*.flac",
    "*.m4a",
    "*.aac",
    "*.iso",
    "*.img",
    "*.qcow2",
    "*.vmdk",
    "*.vdi",
    "*.vhd",
    "*.zip",
    "*.tar",
    "*.tar.gz",
    "*.tgz",
    "*.tar.bz2",
    "*.tar.xz",
    "*.7z",
    "*.rar",
    "*.zst",
    "*.pt",
    "*.pth",
    "*.ckpt",
    "*.safetensors",
    "*.onnx",
    "*.gguf",
    "*.ggml",
    "*.sqlite",
    "*.sqlite3",
    "*.db",
    "*.db-wal",
    "*.db-shm",
    "*.mdb",
    "*.lmdb",
]
EXCLUDES = DEFAULT_EXCLUDES + [
    p for p in os.environ.get("FSTRACK_EXCLUDE", "").split(":") if p
]

START_TS = datetime.now(timezone.utc).isoformat()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(msg: str) -> None:
    print(f"[fstrackd {utcnow()}] {msg}", file=sys.stderr, flush=True)


def slug(path: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", path.strip("/"))
    return s or "root"



def is_excluded(rel: str, name: str) -> bool:
    parts = rel.split("/")
    for pat in EXCLUDES:
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel, pat):
            return True
        if any(fnmatch.fnmatch(part, pat) for part in parts):
            return True
    return False


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _append_index(record: dict) -> None:
    with (OUT / "index.jsonl").open("a") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def _sha256(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


@dataclass
class Scan:
    fingerprint: str
    count: int
    candidates: list[str]
    large: dict[str, dict]
    non_regular: list[str]
    total_bytes: int
    large_bytes: int


class Repo:
    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()
        self.slug = slug(str(self.root))
        self.gitdir = OUT / f"{self.slug}.git"
        self.index_path = OUT / f"{self.slug}.index"
        self.seq_path = OUT / f"{self.slug}.seq"
        self.large_manifest_path = OUT / f"{self.slug}.large.jsonl"
        self.exclude_path = self.gitdir / "info" / "exclude"
        self.last_fp: str | None = None
        self.supports_pathspec_file = False
        self.large_state: dict[str, tuple] = {}
        self.over_cap = False
        self.failures = 0
        self.backoff_until = 0.0
        self.state = {
            "root": str(self.root),
            "gitdir": str(self.gitdir),
            "base": None,
            "head": None,
            "commits": 0,
            "last_error": None,
            "large_files": 0,
            "large_bytes": 0,
            "non_regular": 0,
            "repo_kb": 0,
            "over_cap": False,
            "failures": 0,
            "backoff_sec": 0,
            "max_file_mb": MAX_FILE_MB,
            "large_file_mode": LARGE_MODE,
            "max_repo_mb": MAX_REPO_MB,
            "hash_large_files": HASH_LARGE,
        }

    # ------------------------------------------------------------------ git

    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "GIT_DIR": str(self.gitdir),
                "GIT_WORK_TREE": str(self.root),
                "GIT_INDEX_FILE": str(self.index_path),
                "GIT_AUTHOR_NAME": "fstrack",
                "GIT_AUTHOR_EMAIL": "fstrack@localhost",
                "GIT_COMMITTER_NAME": "fstrack",
                "GIT_COMMITTER_EMAIL": "fstrack@localhost",
                "GIT_CONFIG_NOSYSTEM": "1",
                "HOME": str(OUT),
            }
        )
        env.pop("GIT_PREFIX", None)
        return env

    def _clear_stale_lock(self) -> None:
        """The daemon is the only writer; any surviving lock is stale."""
        if not self.gitdir.exists():
            return
        lock = self.gitdir / "index.lock"
        try:
            if lock.exists():
                lock.unlink()
                log(f"{self.root}: removed stale index.lock")
        except OSError as exc:
            log(f"{self.root}: could not remove index.lock: {exc}")

    def _run(self, args: list[str], *, input_data=None, text: bool = True,
             timeout: int | None = None):
        cmd = ["git", *args]
        try:
            p = subprocess.run(
                cmd,
                cwd=str(self.root),
                env=self.env(),
                input=input_data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=text,
                timeout=timeout or GIT_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._clear_stale_lock()
            empty = "" if text else b""
            return subprocess.CompletedProcess(
                cmd, 124, empty, f"{type(exc).__name__}: {exc}" if text else b"timeout"
            )
        if p.returncode != 0:
            err = p.stderr or ("" if text else b"")
            needle = "index.lock" if text else b"index.lock"
            if needle in err:
                self._clear_stale_lock()
        return p

    def git(self, args: list[str], **kwargs):
        return self._run(args, text=True, **kwargs)

    def git_bytes(self, args: list[str], **kwargs):
        return self._run(args, text=False, **kwargs)

    def init(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.gitdir.parent.mkdir(parents=True, exist_ok=True)
        if not (self.gitdir / "HEAD").exists():
            env = os.environ.copy()
            for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
                env.pop(key, None)
            p = subprocess.run(
                ["git", "init", "--bare", "-q", str(self.gitdir)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if p.returncode != 0:
                raise RuntimeError(f"git init failed: {p.stderr.strip()}")
        for key, value in (
            ("core.hooksPath", "/dev/null"),
            ("core.autocrlf", "false"),
            ("gc.auto", "0"),
            ("user.name", "fstrack"),
            ("user.email", "fstrack@localhost"),
        ):
            self.git(["config", key, value])
        self.exclude_path.parent.mkdir(parents=True, exist_ok=True)
        self.exclude_path.write_text("\n".join(EXCLUDES) + "\n")
        self._clear_stale_lock()

        probe = self.git(["add", "-h"])
        self.supports_pathspec_file = "--pathspec-from-file" in (
            (probe.stdout or "") + (probe.stderr or "")
        )
        self._load_large_state()

        if self.state["base"] is None:
            scan = self.scan()
            self.snapshot("base", allow_empty=True, tag="fstrack/base", scan=scan)
            self.state["base"] = self.state["head"]
            log(f"{self.root}: base {self.state['base']}")

    # ------------------------------------------------------------------ scan

    def scan(self) -> Scan:
        entries: list[tuple] = []
        candidates: list[str] = []
        large: dict[str, dict] = {}
        non_regular: list[str] = []
        total_bytes = 0
        large_bytes = 0

        stack: list[tuple[str, str]] = [(str(self.root), "")]
        while stack:
            dirpath, rel_dir = stack.pop()
            try:
                dir_entries = list(os.scandir(dirpath))
            except OSError:
                continue
            for entry in dir_entries:
                rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                if is_excluded(rel, entry.name):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                mode = st.st_mode
                if stat.S_ISLNK(mode):
                    entries.append(
                        (rel, st.st_mtime_ns, st.st_ctime_ns, st.st_size, mode)
                    )
                    candidates.append(rel)
                    total_bytes += st.st_size
                    continue
                if stat.S_ISDIR(mode):
                    stack.append((entry.path, rel))
                    continue
                if not stat.S_ISREG(mode):
                    non_regular.append(rel)
                    continue
                entries.append(
                    (rel, st.st_mtime_ns, st.st_ctime_ns, st.st_size, mode)
                )
                if LARGE_MODE != "full" and MAX_BYTES > 0 and st.st_size > MAX_BYTES:
                    large[rel] = {
                        "size": st.st_size,
                        "mtime_ns": st.st_mtime_ns,
                        "ctime_ns": st.st_ctime_ns,
                        "mode": mode,
                    }
                    large_bytes += st.st_size
                else:
                    candidates.append(rel)
                    total_bytes += st.st_size

        entries.sort()
        h = hashlib.blake2b(digest_size=16)
        for entry in entries:
            h.update(repr(entry).encode())
        return Scan(
            fingerprint=h.hexdigest(),
            count=len(entries),
            candidates=sorted(candidates),
            large=large,
            non_regular=sorted(non_regular),
            total_bytes=total_bytes,
            large_bytes=large_bytes,
        )

    # -------------------------------------------------------------- staging

    @staticmethod
    def _pathspec(path: str) -> str:
        # `:(literal)` stops git from interpreting glob/`:magic` characters in
        # arbitrary task filenames.
        return f":(literal){path}"

    def tracked_paths(self) -> list[str]:
        p = self.git_bytes(["ls-files", "-z"])
        if p.returncode != 0:
            return []
        return [os.fsdecode(x) for x in p.stdout.split(b"\0") if x]

    def add_paths(self, paths: list[str]):
        if not paths:
            return subprocess.CompletedProcess(["git", "add"], 0, "", "")
        if self.supports_pathspec_file:
            data = b"".join(
                b":(literal)" + os.fsencode(p) + b"\0" for p in paths
            )
            p = self.git_bytes(
                ["add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul"],
                input_data=data,
            )
            if p.returncode == 0:
                return p
            log(
                f"{self.root}: pathspec-file add failed, using argv fallback: "
                f"{p.stderr[-200:] if isinstance(p.stderr, bytes) else p.stderr}"
            )
        for i in range(0, len(paths), 500):
            chunk = [self._pathspec(p) for p in paths[i : i + 500]]
            p = self.git(["add", "-A", "--", *chunk])
            if p.returncode != 0:
                return p
        return subprocess.CompletedProcess(["git", "add"], 0, "", "")

    def remove_paths(self, paths: list[str]):
        for i in range(0, len(paths), 500):
            chunk = [self._pathspec(p) for p in paths[i : i + 500]]
            p = self.git(["rm", "--cached", "--ignore-unmatch", "--", *chunk])
            if p.returncode != 0:
                return p
        return subprocess.CompletedProcess(["git", "rm"], 0, "", "")

    # ------------------------------------------------------- large manifest

    def _load_large_state(self) -> None:
        if not self.large_manifest_path.exists():
            return
        try:
            with self.large_manifest_path.open() as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    path = rec.get("path")
                    if not path:
                        continue
                    if rec.get("state") == "large" and all(
                        k in rec for k in ("size", "mtime_ns", "ctime_ns", "mode")
                    ):
                        self.large_state[path] = (
                            rec["size"],
                            rec["mtime_ns"],
                            rec["ctime_ns"],
                            rec["mode"],
                        )
                    else:
                        self.large_state.pop(path, None)
        except OSError as exc:
            log(f"{self.root}: could not read large manifest: {exc}")

    def _write_large_manifest(self, scan: Scan, trigger: str,
                              event: dict | None) -> None:
        if LARGE_MODE != "manifest":
            return
        wall = utcnow()
        ev = {
            k: event.get(k)
            for k in ("kind", "tool", "toolCallId", "turn", "sessionId", "leafId")
            if event and event.get(k) is not None
        }
        candidate_set = set(scan.candidates)
        lines: list[dict] = []
        new_state = dict(self.large_state)
        removed: list[str] = []
        for path in sorted(scan.large):
            info = scan.large[path]
            key = (info["size"], info["mtime_ns"], info["ctime_ns"], info["mode"])
            if self.large_state.get(path) == key:
                continue
            rec = {
                "wall": wall,
                "trigger": trigger,
                "path": path,
                "state": "large",
                **info,
                **ev,
            }
            if HASH_LARGE:
                try:
                    rec["sha256"] = _sha256(self.root / path)
                except OSError:
                    pass
            lines.append(rec)
            new_state[path] = key
        for path in sorted(set(self.large_state) - set(scan.large)):
            if path in candidate_set:
                state = "tracked"
            elif (self.root / path).exists():
                state = "untracked"
            else:
                state = "deleted"
            lines.append(
                {"wall": wall, "trigger": trigger, "path": path, "state": state, **ev}
            )
            removed.append(path)
        if not lines:
            return
        try:
            with self.large_manifest_path.open("a") as fh:
                for rec in lines:
                    fh.write(json.dumps(rec, sort_keys=True) + "\n")
        except OSError as exc:
            # Keep the old state so a later snapshot retries the manifest write.
            log(f"{self.root}: could not write large manifest: {exc}")
            return
        self.large_state = new_state
        for path in removed:
            self.large_state.pop(path, None)

    # ------------------------------------------------------------- commits

    def next_seq(self) -> int:
        seq = 0
        try:
            seq = int(self.seq_path.read_text().strip())
        except (OSError, ValueError):
            pass
        seq += 1
        _atomic_write(self.seq_path, str(seq))
        return seq

    def task_head(self) -> str | None:
        """HEAD of the task's *own* repo, if it has one. Metadata only."""
        env = os.environ.copy()
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            env.pop(key, None)
        try:
            p = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.root),
                    "-c",
                    "safe.directory=*",
                    "rev-parse",
                    "HEAD",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            )
            return p.stdout.strip() if p.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    def commit_message(self, seq: int, trigger: str, event: dict | None,
                       scan: Scan) -> str:
        lines = [f"fstrack {seq:06d} {trigger}", ""]
        lines.append(f"fstrack-seq: {seq}")
        lines.append(f"fstrack-root: {self.root}")
        lines.append(f"fstrack-trigger: {trigger}")
        lines.append(f"fstrack-wall: {utcnow()}")
        lines.append(f"fstrack-large-files: {len(scan.large)}")
        lines.append(f"fstrack-large-bytes: {scan.large_bytes}")
        lines.append(f"fstrack-repo-kb: {self.state.get('repo_kb', 0)}")
        lines.append(f"fstrack-over-cap: {str(self.over_cap).lower()}")
        if event:
            for key in (
                "kind",
                "tool",
                "toolCallId",
                "turn",
                "sessionId",
                "leafId",
                "isError",
            ):
                value = event.get(key)
                if value is not None:
                    lines.append(f"fstrack-{key}: {value}")
            inp = event.get("input")
            if isinstance(inp, dict):
                for key in ("path", "file_path", "command"):
                    value = inp.get(key)
                    if isinstance(value, str):
                        value = value.replace("\n", " ")[:500]
                        lines.append(f"fstrack-input-{key}: {value}")
        head = self.task_head()
        if head:
            lines.append(f"fstrack-task-head: {head}")
        return "\n".join(lines) + "\n"

    def changed_files(self) -> list[str]:
        p = self.git(
            ["diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "HEAD"]
        )
        return [line for line in p.stdout.splitlines() if line] if p.returncode == 0 else []

    def _repo_kb(self) -> int:
        p = self.git(["count-objects", "-v"])
        if p.returncode != 0:
            return 0
        total = 0
        for line in p.stdout.splitlines():
            if line.startswith(("size:", "size-pack:")):
                try:
                    total += int(line.split()[1])
                except (IndexError, ValueError):
                    pass
        return total

    def _update_policy(self, scan: Scan) -> None:
        self.state["large_files"] = len(scan.large)
        self.state["large_bytes"] = scan.large_bytes
        self.state["non_regular"] = len(scan.non_regular)
        self.state["repo_kb"] = self._repo_kb()
        if MAX_REPO_BYTES and self.state["repo_kb"] * 1024 > MAX_REPO_BYTES:
            if not self.over_cap:
                log(
                    f"{self.root}: repo is {self.state['repo_kb']} KiB, over "
                    f"max_repo_mb={MAX_REPO_MB}; disabling content commits"
                )
            self.over_cap = True
        self.state["over_cap"] = self.over_cap
        self.state["failures"] = self.failures
        self.state["backoff_sec"] = max(0, int(self.backoff_until - time.time()))

    def _fail(self, message: str) -> None:
        self.state["last_error"] = message[-2000:]
        self.failures += 1
        self.backoff_until = time.time() + min(60, 2 ** min(self.failures, 6))
        self.state["failures"] = self.failures
        self.state["backoff_sec"] = int(self.backoff_until - time.time())
        self._clear_stale_lock()
        log(f"{self.root}: {message[-500:]}")
        write_summary()

    def snapshot(
        self,
        trigger: str,
        event: dict | None = None,
        allow_empty: bool = False,
        tag: str | None = None,
        scan: Scan | None = None,
    ) -> bool:
        lock_path = OUT / ".fstrack.lock"
        with lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                if scan is None:
                    scan = self.scan()
                self._write_large_manifest(scan, trigger, event)

                if self.over_cap:
                    if not allow_empty:
                        self.last_fp = scan.fingerprint
                        self._update_policy(scan)
                        return False
                elif MAX_REPO_BYTES and scan.total_bytes > MAX_REPO_BYTES:
                    if not self.over_cap:
                        log(
                            f"{self.root}: current tree is {scan.total_bytes} bytes, "
                            f"over max_repo_mb={MAX_REPO_MB}; disabling content commits"
                        )
                    self.over_cap = True
                    self.state["over_cap"] = True
                    if not allow_empty:
                        self.last_fp = scan.fingerprint
                        self._update_policy(scan)
                        return False
                else:
                    tracked = self.tracked_paths()
                    tracked_set = set(tracked)
                    large_tracked = [p for p in tracked if p in scan.large]
                    nonreg_tracked = [
                        p for p in tracked if p in set(scan.non_regular)
                    ]
                    # A tracked path that now matches an exclude (or lives under
                    # an excluded directory) must leave the index; otherwise the
                    # explicit `git add` pathspec would re-add an ignored file.
                    excluded_tracked = [
                        p
                        for p in tracked
                        if is_excluded(p, os.path.basename(p))
                    ]
                    remove = sorted(
                        set(large_tracked)
                        | set(nonreg_tracked)
                        | set(excluded_tracked)
                    )
                    if remove:
                        r = self.remove_paths(remove)
                        if r.returncode != 0:
                            self._fail(f"git rm --cached failed: {r.stderr}")
                            return False
                        tracked_set -= set(remove)
                    to_add = sorted(set(scan.candidates) | tracked_set)
                    a = self.add_paths(to_add)
                    if a.returncode != 0:
                        self._fail(f"git add failed: {a.stderr}")
                        return False
                    diff = self.git(["diff", "--cached", "--quiet"])
                    if diff.returncode == 0 and not allow_empty:
                        self.last_fp = scan.fingerprint
                        self._update_policy(scan)
                        return False
                    if diff.returncode not in (0, 1):
                        self._fail(f"git diff --cached failed: {diff.stderr}")
                        return False

                seq = self.next_seq()
                message = self.commit_message(seq, trigger, event, scan)
                msg_path = OUT / f".commit-{os.getpid()}.msg"
                msg_path.write_text(message)
                try:
                    commit = self.git(
                        [
                            "commit",
                            "--allow-empty",
                            "--no-verify",
                            "-q",
                            "-F",
                            str(msg_path),
                        ]
                    )
                finally:
                    try:
                        msg_path.unlink()
                    except OSError:
                        pass
                if commit.returncode != 0:
                    self._fail(f"git commit failed: {commit.stderr}")
                    return False

                head = self.git(["rev-parse", "HEAD"]).stdout.strip()
                changed = self.changed_files()
                self.state["head"] = head
                self.state["commits"] = seq
                self.state["last_error"] = None
                if tag:
                    self.git(["tag", "-f", tag, head])
                self._update_policy(scan)
                _append_index(
                    {
                        "seq": seq,
                        "root": str(self.root),
                        "commit": head,
                        "trigger": trigger,
                        "wall": utcnow(),
                        "changed": len(changed),
                        "files": changed[:200],
                        "large_files": len(scan.large),
                        "large_bytes": scan.large_bytes,
                        "repo_kb": self.state["repo_kb"],
                        "over_cap": self.over_cap,
                        "event": {
                            k: event.get(k)
                            for k in (
                                "kind",
                                "tool",
                                "toolCallId",
                                "turn",
                                "sessionId",
                                "leafId",
                            )
                            if event and event.get(k) is not None
                        },
                    }
                )
                self.last_fp = scan.fingerprint
                self.failures = 0
                self.backoff_until = 0.0
                self.state["failures"] = 0
                self.state["backoff_sec"] = 0
                write_summary()
                return True
            except Exception as exc:
                self._fail(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
                return False
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


REPOS: list[Repo] = []
EVENTS_SEEN = 0


def write_summary(finished: bool = False) -> None:
    payload = {
        "version": 2,
        "started_at": START_TS,
        "updated_at": utcnow(),
        "finished_at": utcnow() if finished else None,
        "policy": {
            "max_file_mb": MAX_FILE_MB,
            "large_file_mode": LARGE_MODE,
            "max_repo_mb": MAX_REPO_MB,
            "hash_large_files": HASH_LARGE,
            "git_timeout_sec": GIT_TIMEOUT,
        },
        "roots": {r.state["root"]: r.state for r in REPOS},
        "events": EVENTS_SEEN,
        "out": str(OUT),
    }
    _atomic_write(OUT / "summary.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")


def handle_event(event: dict) -> None:
    global EVENTS_SEEN
    EVENTS_SEEN += 1
    kind = event.get("kind")
    if kind == "tool_result":
        # Only commit when the tool actually changed something.
        for repo in REPOS:
            repo.snapshot("tool_result", event, allow_empty=False)
    elif kind == "turn_end":
        turn = int(event.get("turn") or 0)
        for repo in REPOS:
            repo.snapshot(
                "turn_end", event, allow_empty=True, tag=f"fstrack/turn-{turn:04d}"
            )
    elif kind == "agent_settled":
        for repo in REPOS:
            repo.snapshot("agent_settled", event, allow_empty=True)
    elif kind == "session_start":
        for repo in REPOS:
            repo.snapshot("session_start", event, allow_empty=True)
    # tool_call is informational; the pre-state is already the previous commit.


def main() -> int:
    global REPOS
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "events.jsonl").touch(exist_ok=True)
    log(
        f"start out={OUT} roots={ROOTS} scan={SCAN_INTERVAL}s "
        f"max_file_mb={MAX_FILE_MB} large_mode={LARGE_MODE} "
        f"max_repo_mb={MAX_REPO_MB} hash_large={HASH_LARGE}"
    )

    REPOS = [Repo(root) for root in ROOTS]
    for repo in REPOS:
        try:
            repo.init()
        except Exception as exc:
            repo.state["last_error"] = f"{type(exc).__name__}: {exc}"
            log(f"{repo.root}: init failed: {exc}")
            log(traceback.format_exc())
    write_summary()

    stop = {"flag": False}

    def on_signal(signum, _frame):
        log(f"signal {signum}; stopping")
        stop["flag"] = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    events_path = OUT / "events.jsonl"
    offset = 0
    buffer = ""
    next_scan = 0.0

    while not stop["flag"]:
        # 1. drain new events (cheap; every POLL_INTERVAL)
        try:
            if events_path.exists():
                with events_path.open("r") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
                    offset = fh.tell()
                if chunk:
                    buffer += chunk
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            log(f"bad event: {line[:200]}")
                            continue
                        handle_event(event)
                        next_scan = 0.0
        except OSError as exc:
            log(f"event read failed: {exc}")

        # 2. periodic scan for non-tool changes
        now = time.time()
        if now >= next_scan:
            for repo in REPOS:
                if now < repo.backoff_until:
                    continue
                try:
                    scan = repo.scan()
                    if scan.fingerprint != repo.last_fp:
                        repo.snapshot("fs-change", scan=scan)
                except Exception as exc:
                    repo._fail(f"scan failed: {type(exc).__name__}: {exc}")
            next_scan = now + SCAN_INTERVAL

        time.sleep(POLL_INTERVAL)

    # 3. final flush
    for repo in REPOS:
        try:
            repo.snapshot("final", allow_empty=True, tag="fstrack/final")
        except Exception as exc:
            repo._fail(f"final failed: {type(exc).__name__}: {exc}")
    write_summary(finished=True)
    log("stopped")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
