# SmartApp Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a maintainable Python 3.8-compatible SmartApp Runtime that securely installs one application at a time, supervises its backend and renderer, serves its web assets, routes local messages, and recovers cleanly from failures.

**Architecture:** Implement a modular monolith with a single actor-style `RuntimeCoordinator`. Domain and application modules depend only on ports; filesystem, HTTPS, subprocess, renderer, static HTTP, persistence, and Unix-socket concerns live in adapters/infrastructure. Long-running work reports immutable events back to the coordinator so only one task mutates session state.

**Tech Stack:** Python 3.8–3.14, standard library (`asyncio`, `dataclasses`, `enum`, `http.server`, `tarfile`, `urllib`, `unittest`) plus conditional `tomli` on Python < 3.11.

**Spec:** `docs/superpowers/specs/2026-09-18-smartapp-runtime-design.md`

## Global Constraints

- `pyproject.toml` must declare `requires-python = ">=3.8"`.
- Runtime code must parse and run on Python 3.8: no `match`, `StrEnum`, `TaskGroup`, `asyncio.timeout`, `asyncio.to_thread`, `Path.is_relative_to`, PEP 604 annotations, or `dataclass(slots=True)`.
- Use `tomllib` on Python >= 3.11 and conditional dependency `tomli>=2.0.1,<3` on Python 3.8–3.10.
- Production networking binds only to loopback; management uses a permission-protected Unix socket.
- Never use `shell=True`; backend and renderer commands are argv arrays.
- Installation is fail-closed, immutable by app/version, SHA-256 verified, and safe against archive path/link/special-file attacks.
- Automated tests use temporary directories, random ports, fake adapters, and no public network, ROS2, Electron, camera, or `/data/smartapp`.
- Preserve all pre-existing user changes. Do not create a branch, worktree, commit, or push.
- New Python modules use focused responsibilities, typed public interfaces, and concise English identifiers. User-facing documentation remains Simplified Chinese.

## File Map

```text
smartapp-runtime/
├── pyproject.toml
├── README.md
├── config/runtime.example.toml
├── config/smartapp-runtime.service
├── examples/agent_client.py
├── src/smartapp_runtime/
│   ├── __init__.py, __main__.py, bootstrap.py, config.py, logging.py
│   ├── domain/commands.py, errors.py, manifest.py, models.py, state.py
│   ├── application/coordinator.py, lifecycle.py, router.py
│   ├── ports/agent.py, downloader.py, process.py, renderer.py, repository.py, static_server.py
│   ├── infrastructure/ipc/server.py
│   ├── infrastructure/packages/archive.py, downloader.py, installer.py
│   ├── infrastructure/persistence/lock.py, paths.py, pointers.py, recovery.py, state_repository.py
│   ├── infrastructure/processes/supervisor.py
│   └── infrastructure/web/server.py
└── tests/unit/, tests/integration/, tests/e2e/, tests/fixtures/
```

Every package directory receives an `__init__.py`. Empty re-export files stay empty until a public symbol is intentionally exported.

---

### Task 1: Project Foundation, Errors, State, and Configuration

**Files:**
- Create: `smartapp-runtime/pyproject.toml`
- Create: `smartapp-runtime/src/smartapp_runtime/__init__.py`
- Create: `smartapp-runtime/src/smartapp_runtime/domain/errors.py`
- Create: `smartapp-runtime/src/smartapp_runtime/domain/state.py`
- Create: `smartapp-runtime/src/smartapp_runtime/config.py`
- Test: `smartapp-runtime/tests/unit/test_config.py`
- Test: `smartapp-runtime/tests/unit/test_errors_and_state.py`

**Interfaces:**
- Produces: `ErrorCode`, `SmartAppError`, `RuntimeState`, `RuntimeConfig`, `load_config(path)`.
- Consumes: no earlier task.

- [ ] **Step 1: Create package metadata and the failing Python-version/config tests**

`pyproject.toml` must contain:

```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "smartapp-runtime"
version = "0.1.0"
requires-python = ">=3.8"
dependencies = [
  "tomli>=2.0.1,<3; python_version < '3.11'",
]

[project.scripts]
smartapp-runtime = "smartapp_runtime.__main__:main"

[tool.setuptools]
package-dir = {"" = "src"}

[tool.setuptools.packages.find]
where = ["src"]
```

Test concrete defaults and unknown-key rejection:

```python
class ConfigTests(unittest.TestCase):
    def test_loads_python38_compatible_toml(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "runtime.toml"
            path.write_text('[paths]\nroot = "' + raw + '/data"\n', encoding="utf-8")
            config = load_config(path)
            self.assertEqual(config.paths.root, root / "data")
            self.assertEqual(config.network.static_host, "127.0.0.1")
            self.assertEqual(config.limits.max_message_bytes, 1024 * 1024)

    def test_rejects_unknown_top_level_key(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "runtime.toml"
            path.write_text('[unknown]\nvalue = 1\n', encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "unknown configuration section"):
                load_config(path)
```

- [ ] **Step 2: Run the focused tests and confirm the import failure**

Run:

```bash
PYTHONPATH="smartapp-runtime/src" python3 -m unittest discover \
  -s "smartapp-runtime/tests/unit" -p "test_*.py" -v
```

Expected: FAIL because `smartapp_runtime.config` and domain modules do not exist.

- [ ] **Step 3: Implement stable errors and runtime states**

Use string enums compatible with Python 3.8:

```python
class ErrorCode(str, Enum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNSUPPORTED_SCHEMA = "UNSUPPORTED_SCHEMA"
    SESSION_CONFLICT = "SESSION_CONFLICT"
    SESSION_MISMATCH = "SESSION_MISMATCH"
    SEQ_OUT_OF_ORDER = "SEQ_OUT_OF_ORDER"
    QUEUE_FULL = "QUEUE_FULL"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    PACKAGE_TOO_LARGE = "PACKAGE_TOO_LARGE"
    SIZE_MISMATCH = "SIZE_MISMATCH"
    HASH_MISMATCH = "HASH_MISMATCH"
    ARCHIVE_UNSAFE = "ARCHIVE_UNSAFE"
    MANIFEST_INVALID = "MANIFEST_INVALID"
    INSTALL_CONFLICT = "INSTALL_CONFLICT"
    PORT_IN_USE = "PORT_IN_USE"
    START_TIMEOUT = "START_TIMEOUT"
    BACKEND_EXITED = "BACKEND_EXITED"
    BACKEND_PROTOCOL_ERROR = "BACKEND_PROTOCOL_ERROR"
    RENDERER_FAILED = "RENDERER_FAILED"
    UPSTREAM_QUEUE_FULL = "UPSTREAM_QUEUE_FULL"
    RECOVERY_FAILED = "RECOVERY_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
```

`SmartAppError` carries `code`, sanitized `message`, and a copied `details` dict. `RuntimeState` contains exactly the ten states in the design.

- [ ] **Step 4: Implement strict TOML loading with Python 3.8 fallback**

Use explicit nested dataclasses: `PathsConfig`, `NetworkConfig`, `TimeoutConfig`, `LimitConfig`, `ProcessConfig`, `RendererConfig`, `LoggingConfig`, and `RuntimeConfig`. Load TOML with:

```python
try:
    import tomllib
except ImportError:
    import tomli as tomllib
```

Reject unknown sections/keys, non-loopback production hosts, invalid ports, non-positive limits, and invalid renderer kinds. Do not silently coerce strings to integers or booleans.

- [ ] **Step 5: Run foundation tests and compile the package**

Run:

```bash
PYTHONPATH="smartapp-runtime/src" python3 -m unittest discover \
  -s "smartapp-runtime/tests/unit" -p "test_config.py" -v
PYTHONPATH="smartapp-runtime/src" python3 -m compileall -q "smartapp-runtime/src"
```

Expected: all focused tests pass and `compileall` exits 0.

- [ ] **Step 6: Review checkpoint**

Inspect `git diff -- smartapp-runtime/pyproject.toml smartapp-runtime/src/smartapp_runtime smartapp-runtime/tests/unit`; do not commit.

---

### Task 2: Commands, Identifiers, Session Models, and Manifest v1

**Files:**
- Create: `smartapp-runtime/src/smartapp_runtime/domain/commands.py`
- Create: `smartapp-runtime/src/smartapp_runtime/domain/models.py`
- Create: `smartapp-runtime/src/smartapp_runtime/domain/manifest.py`
- Test: `smartapp-runtime/tests/unit/test_commands.py`
- Test: `smartapp-runtime/tests/unit/test_manifest.py`

**Interfaces:**
- Consumes: `SmartAppError`, `ErrorCode` from Task 1.
- Produces: `StartApp.from_dict`, `StopApp.from_dict`, `CloudData.from_dict`, `GetStatus.from_dict`, `Session`, `Manifest.from_dict`, `load_manifest(path)`.

- [ ] **Step 1: Write failing identifier and command-schema tests**

Cover valid start, invalid `appId`, invalid SHA length, boolean-as-integer rejection, unknown field rejection, stop reason enum, target enum, and `seq` range. Include this exact conflict assertion:

```python
def test_start_rejects_unknown_md5_field(self):
    payload = valid_start_payload()
    payload["md5"] = "abc"
    with self.assertRaisesRegex(SmartAppError, "unknown field"):
        StartApp.from_dict(payload)
```

- [ ] **Step 2: Write failing Manifest v1 tests**

Use table-driven cases for Python-only, Web-only, Hybrid, no enabled component, mismatched `appId/version`, unknown keys, invalid entry path, and invalid Hybrid default target.

- [ ] **Step 3: Run tests and confirm missing domain types**

Run:

```bash
PYTHONPATH="smartapp-runtime/src" python3 -m unittest discover \
  -s "smartapp-runtime/tests/unit" -p "test_*.py" -v
```

Expected: FAIL on missing imports.

- [ ] **Step 4: Implement strict mapping parsers**

Each parser first checks `type(value)`, then required keys, then exact allowed keys. Use `re.fullmatch` with the three identifier patterns from the spec. Model enums as `class StopReason(str, Enum)` and `class MessageTarget(str, Enum)`.

Use immutable dataclasses (`@dataclass(frozen=True)`, without slots). Copy mutable `init_data` and `data` at construction so callers cannot mutate accepted commands.

- [ ] **Step 5: Implement Manifest v1 routing rules**

Expose:

```python
@dataclass(frozen=True)
class ComponentConfig:
    enabled: bool
    entry: str

@dataclass(frozen=True)
class BackendConfig(ComponentConfig):
    dynamic_service: bool

@dataclass(frozen=True)
class Manifest:
    schema_version: int
    app_id: str
    version: str
    web: ComponentConfig
    backend: BackendConfig
    default_target: MessageTarget
```

Validate entry paths with `PurePosixPath`: reject absolute paths, empty segments, `.` and `..`. Filesystem containment is rechecked after installation in Task 3.

- [ ] **Step 6: Run domain tests**

Run:

```bash
PYTHONPATH="smartapp-runtime/src" python3 -m unittest discover \
  -s "smartapp-runtime/tests/unit" -p "test_*.py" -v
```

Expected: all command and manifest tests pass.

- [ ] **Step 7: Review checkpoint**

Check that public field names use `app_id` internally and serialize as `appId`; do not add aliases for `appid`, `cmccAppId`, or `md5` inside Runtime.

---

### Task 3: HTTPS Download, Safe Archive Extraction, and Atomic Installation

**Files:**
- Create: `smartapp-runtime/src/smartapp_runtime/ports/downloader.py`
- Create: `smartapp-runtime/src/smartapp_runtime/infrastructure/packages/downloader.py`
- Create: `smartapp-runtime/src/smartapp_runtime/infrastructure/packages/archive.py`
- Create: `smartapp-runtime/src/smartapp_runtime/infrastructure/packages/installer.py`
- Test: `smartapp-runtime/tests/unit/test_archive.py`
- Test: `smartapp-runtime/tests/integration/test_installer.py`

**Interfaces:**
- Consumes: `StartApp`, `Manifest`, `RuntimeConfig`, `SmartAppError`.
- Produces: `DownloadRequest`, `DownloadResult`, `Downloader`, `HttpsDownloader`, `SafeArchiveExtractor.extract`, `InstalledApp`, `PackageInstaller.ensure_installed`.

- [ ] **Step 1: Write malicious tar tests before extraction code**

Generate tar files in memory for: valid single-root archive, `../escape`, absolute path, symlink, hardlink, FIFO, duplicate path, wrong root, too many files, oversized member, and manifest mismatch. Assert `ARCHIVE_UNSAFE` or `MANIFEST_INVALID` and assert no path outside the temporary staging root exists.

- [ ] **Step 2: Run archive tests and confirm failure**

Run:

```bash
PYTHONPATH="smartapp-runtime/src" python3 -m unittest discover \
  -s "smartapp-runtime/tests/unit" -p "test_archive.py" -v
```

Expected: FAIL because `SafeArchiveExtractor` is missing.

- [ ] **Step 3: Implement the safe extractor without `extractall`**

For every `TarInfo`:

```python
relative = PurePosixPath(member.name)
if relative.is_absolute() or ".." in relative.parts:
    raise SmartAppError(ErrorCode.ARCHIVE_UNSAFE, "archive path escapes package root")
if not (member.isdir() or member.isfile()):
    raise SmartAppError(ErrorCode.ARCHIVE_UNSAFE, "archive contains unsupported member type")
target = (staging_root / Path(*relative.parts)).resolve()
try:
    target.relative_to(staging_root.resolve())
except ValueError:
    raise SmartAppError(ErrorCode.ARCHIVE_UNSAFE, "archive path escapes staging root")
```

Track normalized names, count, per-file size, total declared size, and actual copied bytes. Create directories as `0755`, files with exclusive create and `0644`, and reject setuid/setgid bits.

- [ ] **Step 4: Write installer tests with a deterministic FakeDownloader**

Assert first install downloads once, second identical request is a cache hit, wrong size/hash removes `.part`, manifest mismatch removes staging, same version/different hash returns `INSTALL_CONFLICT`, and a successful install writes `.smartapp-install.json` before atomic rename.

- [ ] **Step 5: Implement downloader and installer ports**

Define:

```python
class Downloader(Protocol):
    async def download(self, request, destination):
        # type: (DownloadRequest, Path) -> DownloadResult
        ...

@dataclass(frozen=True)
class InstalledApp:
    root: Path
    manifest: Manifest
    sha256: str
    package_size: int
    cache_hit: bool
```

The protocol body is never called; concrete classes implement it. `HttpsDownloader` runs its synchronous urllib loop through `loop.run_in_executor`, enforces HTTPS on the initial URL and every redirect, caps redirects at 3, streams into `.part`, and computes SHA-256 incrementally.

- [ ] **Step 6: Implement installation transaction**

Use randomized `.part` and `.staging` names on the same filesystem as the final directory. Validate installed manifest entries with `resolve()` plus `relative_to()` and `is_file()`. Write and fsync metadata, then `os.replace(staged_app_root, final_version_root)`.

- [ ] **Step 7: Run package tests**

Run:

```bash
PYTHONPATH="smartapp-runtime/src" python3 -m unittest discover \
  -s "smartapp-runtime/tests/unit" -p "test_archive.py" -v
PYTHONPATH="smartapp-runtime/src" python3 -m unittest discover \
  -s "smartapp-runtime/tests/integration" -p "test_installer.py" -v
```

Expected: all package-security and installation tests pass without public network access.

- [ ] **Step 8: Review checkpoint**

Search `rg -n "extractall|shell=True|http://" smartapp-runtime/src/smartapp_runtime/infrastructure/packages`; expected: no unsafe extraction, shell invocation, or production plain-HTTP path.

---

### Task 4: Runtime Paths, Atomic Pointers, State Repository, Lock, and Recovery

**Files:**
- Create: `smartapp-runtime/src/smartapp_runtime/ports/repository.py`
- Create: `smartapp-runtime/src/smartapp_runtime/infrastructure/persistence/paths.py`
- Create: `smartapp-runtime/src/smartapp_runtime/infrastructure/persistence/pointers.py`
- Create: `smartapp-runtime/src/smartapp_runtime/infrastructure/persistence/state_repository.py`
- Create: `smartapp-runtime/src/smartapp_runtime/infrastructure/persistence/lock.py`
- Create: `smartapp-runtime/src/smartapp_runtime/infrastructure/persistence/recovery.py`
- Test: `smartapp-runtime/tests/integration/test_persistence.py`
- Test: `smartapp-runtime/tests/integration/test_recovery.py`

**Interfaces:**
- Consumes: configuration paths, `RuntimeState`, `InstalledApp`.
- Produces: `RuntimePaths`, `PointerSnapshot`, `AtomicPointers`, `PersistedRuntimeState`, `FileStateRepository`, `SingleInstanceLock`, `RecoveryService.recover`.

- [ ] **Step 1: Write failing atomic-pointer and state-write tests**

Verify `current`, `previous`, and global `current_web` updates use temporary sibling symlinks and `os.replace`; snapshots restore exact prior targets; state writes leave valid JSON after repeated replacements; invalid pointers outside the app root are rejected.

- [ ] **Step 2: Implement path creation and pointer containment**

`RuntimePaths.from_root(root)` returns all fixed subpaths and `ensure_layout()` creates only runtime-owned directories. Pointer targets are relative where possible and are accepted only if their resolved target lies below `apps_root`.

- [ ] **Step 3: Implement atomic JSON state persistence**

`FileStateRepository.save(state)` writes UTF-8 JSON to a sibling temporary file, flushes and `os.fsync`s it, replaces the destination, then fsyncs the parent directory. `load()` rejects unknown schema versions and malformed JSON with `RECOVERY_FAILED`.

- [ ] **Step 4: Write failing lock and recovery tests**

Test two lock objects against the same path, stale `.part/.staging` cleanup, valid pointer retention, invalid pointer removal, and PID identity mismatch causing no signal call. Inject `ProcessIdentityReader` and `SignalSender` fakes so tests never signal real processes.

- [ ] **Step 5: Implement safe recovery**

On Linux, require persisted PID, `/proc/<pid>/stat` start time, and cmdline install-root marker all to match before signalling the stored process group. On non-Linux, log a warning and skip PID recovery. Always restore the renderer through its port and persist `IDLE` with no active session.

- [ ] **Step 6: Run persistence tests**

Run:

```bash
PYTHONPATH="smartapp-runtime/src" python3 -m unittest discover \
  -s "smartapp-runtime/tests/integration" -p "test_*.py" -v
```

Expected: all tests pass and no process outside a test fake is signalled.

- [ ] **Step 7: Review checkpoint**

Inspect every delete target. Each cleanup path must first resolve below `downloads`, `tmp`, or the validated runtime root; no broad recursive delete may target the configured root itself.

---

### Task 5: Loopback Static Web Server

**Files:**
- Create: `smartapp-runtime/src/smartapp_runtime/ports/static_server.py`
- Create: `smartapp-runtime/src/smartapp_runtime/infrastructure/web/server.py`
- Test: `smartapp-runtime/tests/integration/test_static_server.py`

**Interfaces:**
- Consumes: `AtomicPointers.current_web_target()` and network config.
- Produces: `StaticWebServer.start()`, `StaticWebServer.stop()`, `StaticWebServer.address`.

- [ ] **Step 1: Write failing HTTP behavior tests**

Start on `127.0.0.1:0`. Assert `/healthz`, `/`, asset MIME type, HEAD without body, 404, POST 405, no directory listing, URL-encoded traversal rejection, symlink escape rejection, `Cache-Control: no-store`, and root switching between two app versions.

- [ ] **Step 2: Run the server test and verify failure**

Run:

```bash
PYTHONPATH="smartapp-runtime/src" python3 -m unittest discover \
  -s "smartapp-runtime/tests/integration" -p "test_static_server.py" -v
```

Expected: FAIL because the server module is absent.

- [ ] **Step 3: Implement a focused `ThreadingHTTPServer` adapter**

Snapshot and resolve `current_web` once per request. Decode with `urllib.parse.unquote`, normalize with `PurePosixPath`, and enforce containment with `relative_to`. Override directory listing to return 404. Only map a directory to its `index.html`.

Run the server in one daemon thread owned by the adapter; `stop()` calls `shutdown()`, `server_close()`, and joins the thread with the configured timeout.

- [ ] **Step 4: Run the HTTP tests twice**

Run the focused test command twice to detect leaked sockets or threads. Expected: both runs pass with no resource warnings.

- [ ] **Step 5: Review checkpoint**

Confirm the production config rejects non-loopback static hosts even though tests may use port `0`.

---

### Task 6: Backend Process Supervisor and JSON Lines Protocol

**Files:**
- Create: `smartapp-runtime/src/smartapp_runtime/ports/process.py`
- Create: `smartapp-runtime/src/smartapp_runtime/infrastructure/processes/supervisor.py`
- Create: `smartapp-runtime/tests/fixtures/backends/ready_backend.py`
- Create: `smartapp-runtime/tests/fixtures/backends/invalid_backend.py`
- Create: `smartapp-runtime/tests/fixtures/backends/tree_backend.py`
- Test: `smartapp-runtime/tests/integration/test_process_supervisor.py`

**Interfaces:**
- Consumes: `InstalledApp`, active session metadata, process/time/limit config.
- Produces: `BackendEvent`, `BackendHandle`, `ProcessSupervisor.start`, `send`, `stop`.

- [ ] **Step 1: Write fixture scripts and failing lifecycle tests**

`ready_backend.py` reads `runtime_init`, emits `app_ready`, echoes `cloud_data` as `app_data`, writes logs to stderr, and exits on `app_stop`. `invalid_backend.py` emits malformed and oversized lines. `tree_backend.py` spawns a sleeping child and records its PID for the test.

Tests cover ready timeout, correct environment, stdout protocol, stderr capture, unexpected exit callback, violation threshold, graceful stop, SIGTERM fallback, SIGKILL fallback, and process-group cleanup.

- [ ] **Step 2: Run the supervisor test and verify failure**

Run:

```bash
PYTHONPATH="smartapp-runtime/src" python3 -m unittest discover \
  -s "smartapp-runtime/tests/integration" -p "test_process_supervisor.py" -v
```

Expected: FAIL on missing supervisor.

- [ ] **Step 3: Implement process creation and readers**

Use:

```python
process = await asyncio.create_subprocess_exec(
    python_executable,
    "-u",
    str(entry),
    cwd=str(installed.root),
    env=controlled_env,
    stdin=asyncio.subprocess.PIPE,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    start_new_session=True,
)
```

Create explicit stdout, stderr, and wait tasks. Read bounded lines and count protocol violations. Store PID identity through the repository port after spawn.

- [ ] **Step 4: Implement idempotent stop escalation**

Send `app_stop`, close stdin after the graceful interval, then signal `os.killpg(pid, SIGTERM)`, then `SIGKILL` after the termination interval. Catch `ProcessLookupError`, always await `process.wait()`, cancel reader tasks, and clear the persisted process identity.

- [ ] **Step 5: Run lifecycle tests and check for leaked fixture PIDs**

Run the test, then have the test itself assert every recorded child PID no longer exists. Expected: all tests pass on POSIX; non-POSIX process-group cases are explicitly skipped.

- [ ] **Step 6: Review checkpoint**

Search `rg -n "create_subprocess_shell|shell=True|os\.system|Popen\(" smartapp-runtime/src`; expected: none.

---

### Task 7: Renderer Ports, Adapters, and Message Router

**Files:**
- Create: `smartapp-runtime/src/smartapp_runtime/ports/renderer.py`
- Create: `smartapp-runtime/src/smartapp_runtime/adapters/fake_renderer.py`
- Create: `smartapp-runtime/src/smartapp_runtime/adapters/command_renderer.py`
- Create: `smartapp-runtime/src/smartapp_runtime/application/router.py`
- Test: `smartapp-runtime/tests/unit/test_router.py`
- Test: `smartapp-runtime/tests/integration/test_renderers.py`

**Interfaces:**
- Consumes: `Manifest`, `CloudData`, backend sender, Agent event sink.
- Produces: `RendererPort`, `FakeRenderer`, `CommandRenderer`, `MessageRouter.begin_session`, `route_cloud_data`, `accept_app_data`, `end_session`.

- [ ] **Step 1: Write failing routing matrix tests**

Cover Python-only/Web-only/Hybrid with `auto`, `python`, `web`, and `broadcast`; disabled-target rejection; old session rejection; duplicate/out-of-order seq; STARTING buffer ordering; 128-message and 4-MiB limits; and app-data enrichment with authoritative session/app IDs.

- [ ] **Step 2: Implement the renderer protocol and fakes**

Define async `load`, `wait_ready`, `send`, `stop`, and `restore_default` methods plus `set_message_handler`. `FakeRenderer` records calls and can be configured to fail or delay readiness.

`CommandRenderer` executes configured argv with `create_subprocess_exec`; placeholders are substituted only as whole argv values (`{url}`, `{session_id}`, `{app_id}`, `{version}`), never through a shell. Exit code 0 is this adapter's ready acknowledgment.

- [ ] **Step 3: Implement bounded router queues**

Track both message count and encoded UTF-8 byte total. During STARTING append accepted data; on RUNNING flush in order. For upstream disconnects, preserve older accepted events and reject the newest event when full with `UPSTREAM_QUEUE_FULL`.

- [ ] **Step 4: Run router and renderer tests**

Run:

```bash
PYTHONPATH="smartapp-runtime/src" python3 -m unittest discover \
  -s "smartapp-runtime/tests/unit" -p "test_router.py" -v
PYTHONPATH="smartapp-runtime/src" python3 -m unittest discover \
  -s "smartapp-runtime/tests/integration" -p "test_renderers.py" -v
```

Expected: routing and adapter tests pass.

- [ ] **Step 5: Review checkpoint**

Verify `MessageRouter` contains no filesystem, subprocess, Unix-socket, HTTP, or Electron imports.

---

### Task 8: Actor-Style Runtime Coordinator and Lifecycle Transaction

**Files:**
- Create: `smartapp-runtime/src/smartapp_runtime/application/lifecycle.py`
- Create: `smartapp-runtime/src/smartapp_runtime/application/coordinator.py`
- Test: `smartapp-runtime/tests/unit/test_coordinator.py`
- Test: `smartapp-runtime/tests/integration/test_lifecycle.py`

**Interfaces:**
- Consumes: installer, pointers, state repository, supervisor, renderer, router, and event sink ports.
- Produces: `RuntimeCoordinator.start`, `submit`, `close`; `CommandResult`; internal `WorkflowProgress`, `WorkflowSucceeded`, `WorkflowFailed`, `ComponentExited` events.

- [ ] **Step 1: Write coordinator tests with deterministic fakes**

Test exact state sequences for cache hit, cold install, install failure, backend-ready timeout, renderer failure, successful RUNNING, duplicate start sharing the original result, same-session conflict, new-session replacement, stop during download, component crash, repeated stop, and stale generation completion.

Each fake exposes an `asyncio.Event` so the test controls when long operations finish; do not use arbitrary sleeps.

- [ ] **Step 2: Run coordinator tests and verify failure**

Run:

```bash
PYTHONPATH="smartapp-runtime/src" python3 -m unittest discover \
  -s "smartapp-runtime/tests/unit" -p "test_coordinator.py" -v
```

Expected: FAIL on missing coordinator.

- [ ] **Step 3: Implement actor event ownership**

`submit(command)` creates a result future and enqueues an external event. The actor loop is the only code allowed to call `_transition`. Long workflows receive an immutable session snapshot, generation, and cancellation event; they report progress/completion through the same queue.

Use an explicit transition table keyed by `RuntimeState`, and raise `INTERNAL_ERROR` for impossible internal transitions rather than silently changing state.

- [ ] **Step 4: Implement startup transaction**

The workflow performs: ensure installed -> validate optional port 18081 -> snapshot pointers -> switch `current_web` if needed -> start backend -> load renderer -> await all readiness -> report success. The actor handles success by updating `previous/current`, entering RUNNING, and flushing the router.

On any exception or cancellation, run the same cleanup stack in reverse order and restore pointer snapshots before reporting failure.

- [ ] **Step 5: Implement replacement and stop semantics**

Keep one `pending_start`; a newer different-session start replaces it and resolves the older pending request with `SESSION_CONFLICT`. Stop cancels the active workflow or enters STOPPING for RUNNING. Cleanup errors are accumulated for logging but never skip subsequent cleanup actions.

- [ ] **Step 6: Run coordinator and lifecycle tests**

Run:

```bash
PYTHONPATH="smartapp-runtime/src" python3 -m unittest discover \
  -s "smartapp-runtime/tests/unit" -p "test_coordinator.py" -v
PYTHONPATH="smartapp-runtime/src" python3 -m unittest discover \
  -s "smartapp-runtime/tests/integration" -p "test_lifecycle.py" -v
```

Expected: all state, cancellation, rollback, and idempotency tests pass.

- [ ] **Step 7: Review checkpoint**

Search assignments to active session/state fields. Expected: mutations occur only inside coordinator actor handlers.

---

### Task 9: Unix Socket Agent Server

**Files:**
- Create: `smartapp-runtime/src/smartapp_runtime/ports/agent.py`
- Create: `smartapp-runtime/src/smartapp_runtime/infrastructure/ipc/server.py`
- Test: `smartapp-runtime/tests/integration/test_ipc_server.py`

**Interfaces:**
- Consumes: `RuntimeCoordinator.submit`, router upstream event stream, command parsers.
- Produces: `AgentServer.start`, `publish`, `stop`, `connected`.

- [ ] **Step 1: Write failing protocol tests**

Test socket mode `0660`, valid start/status/stop correlation, asynchronous `command_result`, malformed JSON, invalid UTF-8, missing newline before size limit, message >1 MiB, second simultaneous client rejection, reconnect after disconnect, and bounded publish queue.

- [ ] **Step 2: Implement strict JSONL framing**

Read with `StreamReader.readline()` under a configured byte limit. Reject incomplete over-limit frames, require a JSON object, parse the `command` discriminator, and keep reader/writer tasks separate so long start operations do not stop input reads.

- [ ] **Step 3: Implement single-client lifecycle**

Create the Unix server, chmod after bind, and store exactly one active connection token. A second client receives one `command_result` with `QUEUE_FULL` and closes. Disconnect clears only connection state; it does not submit `StopApp`.

- [ ] **Step 4: Run IPC tests**

Run:

```bash
PYTHONPATH="smartapp-runtime/src" python3 -m unittest discover \
  -s "smartapp-runtime/tests/integration" -p "test_ipc_server.py" -v
```

Expected: all framing, permissions, correlation, and reconnection tests pass.

- [ ] **Step 5: Review checkpoint**

Verify socket cleanup only unlinks a socket owned by this server and never unlinks an arbitrary regular file at the configured path.

---

### Task 10: Structured Logging, Bootstrap, CLI, and Graceful Daemon Shutdown

**Files:**
- Create: `smartapp-runtime/src/smartapp_runtime/logging.py`
- Create: `smartapp-runtime/src/smartapp_runtime/bootstrap.py`
- Create: `smartapp-runtime/src/smartapp_runtime/__main__.py`
- Test: `smartapp-runtime/tests/unit/test_logging.py`
- Test: `smartapp-runtime/tests/integration/test_bootstrap.py`

**Interfaces:**
- Consumes: every concrete adapter from Tasks 3–9.
- Produces: `configure_logging`, `sanitize_url`, `build_runtime(config)`, `run_runtime(config)`, CLI `main(argv=None)`.

- [ ] **Step 1: Write failing log-redaction and bootstrap-order tests**

Assert URL userinfo/query removal, no full `initData/data` logging, stable correlation fields, JSON-per-line output, and bootstrap order: lock -> layout -> recovery -> static server -> coordinator -> Agent server. Shutdown order is Agent server -> coordinator cleanup -> static server -> lock release.

- [ ] **Step 2: Implement JSON logging without third-party libraries**

Use a custom `logging.Formatter` that emits a fixed JSON object and includes optional correlation attributes. Convert unknown values with `str()` only after excluding payload bodies and secrets.

- [ ] **Step 3: Implement dependency assembly**

`build_runtime` constructs concrete paths, repositories, installer, pointers, static server, process supervisor, configured renderer, router, coordinator, and Agent server. No module-level singleton is allowed; tests can build two isolated runtimes in separate temporary roots.

- [ ] **Step 4: Implement CLI and signals**

Parse only `--config` and `--check-config`. `--check-config` validates and exits without creating runtime directories. On POSIX, register SIGINT/SIGTERM handlers that set an asyncio event; the daemon then performs the documented shutdown order with bounded waits.

- [ ] **Step 5: Run bootstrap tests and CLI smoke checks**

Run:

```bash
PYTHONPATH="smartapp-runtime/src" python3 -m unittest discover \
  -s "smartapp-runtime/tests/unit" -p "test_logging.py" -v
PYTHONPATH="smartapp-runtime/src" python3 -m unittest discover \
  -s "smartapp-runtime/tests/integration" -p "test_bootstrap.py" -v
PYTHONPATH="smartapp-runtime/src" python3 -m smartapp_runtime --help
```

Expected: tests pass and CLI help exits 0.

- [ ] **Step 6: Review checkpoint**

Inspect dependency direction with `rg -n "infrastructure|adapters" smartapp-runtime/src/smartapp_runtime/domain smartapp-runtime/src/smartapp_runtime/application`; domain must have no matches, application imports only ports/domain.

---

### Task 11: Offline End-to-End Fixtures for All Three App Modes

**Files:**
- Create: `smartapp-runtime/tests/fixtures/apps/python_only/`
- Create: `smartapp-runtime/tests/fixtures/apps/web_only/`
- Create: `smartapp-runtime/tests/fixtures/apps/hybrid/`
- Create: `smartapp-runtime/tests/helpers/package_factory.py`
- Create: `smartapp-runtime/tests/e2e/test_runtime_modes.py`
- Create: `smartapp-runtime/tests/e2e/test_runtime_failures.py`

**Interfaces:**
- Consumes: fully assembled runtime and fake downloader/renderer.
- Produces: reusable signed-by-test-metadata tar fixtures and end-to-end acceptance coverage.

- [ ] **Step 1: Build deterministic package fixtures in tests**

`package_factory.py` creates gzip tar bytes with fixed metadata (`mtime=0`, normalized uid/gid/mode), returns exact size/SHA-256, and stores no generated archives in Git. Fixtures include valid manifest and minimal backend/web files.

- [ ] **Step 2: Write success-path E2E tests**

For each mode: submit `StartApp`, await `RUNNING`, assert only declared components started, send init/cloud data, observe app data, issue stop, await `IDLE`, and assert no process/task/socket owned by the session remains. Start the same SHA twice and assert downloader call count remains one.

- [ ] **Step 3: Write failure-path E2E tests**

Cover wrong SHA, unsafe archive, startup timeout, backend crash, renderer failure, new-session replacement, wake-word stop, pointer rollback, Runtime restart cleanup, old-session data, duplicate seq, and STARTING buffer overflow.

- [ ] **Step 4: Run all SmartApp Runtime tests**

Run:

```bash
PYTHONPATH="smartapp-runtime/src" python3 -m unittest discover \
  -s "smartapp-runtime/tests" -p "test_*.py" -v
```

Expected: all unit, integration, and E2E tests pass without public network or hardware.

- [ ] **Step 5: Run tests repeatedly to expose leaks**

Run the full suite three consecutive times. Expected: all runs pass with no address-in-use errors, pending-task warnings, resource warnings, or residual fixture processes.

- [ ] **Step 6: Review checkpoint**

Record test counts and elapsed time in the final handoff; do not claim real Electron/ROS2/RK3588 validation.

---

### Task 12: Documentation, Deployment Files, Python Matrix, and Final Verification

**Files:**
- Create: `smartapp-runtime/README.md`
- Create: `smartapp-runtime/config/runtime.example.toml`
- Create: `smartapp-runtime/config/smartapp-runtime.service`
- Create: `smartapp-runtime/examples/agent_client.py`
- Create: `smartapp-runtime/scripts/check.sh`
- Modify: `README.md`
- Test: all SmartApp Runtime tests and existing repository tests.

**Interfaces:**
- Consumes: final CLI and IPC protocol.
- Produces: operator documentation, example config/service/client, repeatable verification entrypoint.

- [ ] **Step 1: Write the example configuration with every accepted key**

Use a safe local-development root, loopback hosts, documented production defaults, all timeout/limit defaults, explicit environment pass-through, and `renderer.kind = "fake"`. Include a second commented production example using `/data/smartapp` and the command renderer argv arrays.

- [ ] **Step 2: Write the Simplified-Chinese README**

Document architecture boundaries, installation for Python 3.8–3.10 with offline `tomli` wheel, config validation, daemon start/stop, IPC command examples, manifest schema, package layout, error codes, local Fake Renderer limitations, and target-device acceptance checklist.

- [ ] **Step 3: Add systemd and Agent-client examples**

The unit runs the module with an explicit config, dedicated user/group, `Restart=on-failure`, and no restrictions that falsely promise compatibility with unavailable ROS2/GPU devices. `agent_client.py` connects to the Unix socket, sends one JSON line, and prints correlated responses/events.

- [ ] **Step 4: Add a non-destructive verification script**

`scripts/check.sh` uses `set -eu`, resolves its project directory, runs `compileall`, then the full `unittest` suite. It must not install packages, mutate `/data`, or start a persistent daemon.

- [ ] **Step 5: Run available Python interpreters**

For every installed interpreter among `python3.8` through `python3.14`, run:

```bash
PYTHONPATH="smartapp-runtime/src" "$PYTHON" -m compileall -q "smartapp-runtime/src"
PYTHONPATH="smartapp-runtime/src" "$PYTHON" -m unittest discover \
  -s "smartapp-runtime/tests" -p "test_*.py" -v
```

Python 3.8 verification is mandatory before claiming Python 3.8 support. If unavailable locally, run the same commands in a trusted Python 3.8 CI job or container and report that evidence separately.

- [ ] **Step 6: Run existing application regressions**

Run:

```bash
(cd "dog-pc-gestrue" && npm test)
(cd "dog-h5-gesture" && npm test)
(cd "rps-kids-h5" && npm test)
```

Expected: all existing tests remain green; Runtime work must not change the three H5 applications.

- [ ] **Step 7: Run security and architecture scans**

Run:

```bash
rg -n "shell=True|extractall|os\.system|eval\(|exec\(" "smartapp-runtime/src"
rg -n "0\.0\.0\.0|http://" "smartapp-runtime/src"
```

Expected: no unsafe execution/extraction and no production non-loopback/plain-HTTP bindings. Protocol method stubs use `typing.Protocol` with `...`; every concrete adapter has executable behavior and focused tests.

- [ ] **Step 8: Final verification and honest handoff**

Run `smartapp-runtime/scripts/check.sh`, inspect `git diff --check`, summarize created files, tests, Python versions actually executed, and hardware integrations not executed. Do not commit or push.
