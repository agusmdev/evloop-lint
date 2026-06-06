"""Registries: the ONLY place specific identifiers are allowed (ADR-0002/0005).

Every concrete name — blocking primitives, offload primitives, scheduling
primitives, framework registration shapes — lives here as *data*. The resolver
and taint walk read this data; they never hardcode identifiers in logic. This is
what lets a sibling-variant (a different library doing the same thing) be a new
data row instead of a code change, so it survives the generalization holdout.

Matching is by *dotted suffix*: a registry key ``time.sleep`` matches a call
whose resolved dotted target ends with ``time.sleep`` (so ``time.sleep`` and an
``from time import sleep`` -> ``sleep`` both match, the latter via alias
resolution upstream). Single-segment keys (e.g. ``open``) match a bare builtin.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .codes import Code, Confidence


@dataclass(frozen=True)
class BlockerSpec:
    """A known blocking primitive (sync I/O or heavy CPU)."""

    dotted: str                 # e.g. "time.sleep", "requests.get", "socket.socket.recv"
    code: Code
    message: str                # human description of the blocker
    suggested_async_replacement: str = ""  # fix-hint DATA (D6), never logic


@dataclass(frozen=True)
class OffloadSpec:
    """A primitive that runs a callable OFF the event loop (D1).

    ``work_arg_index`` / ``work_arg_keyword`` identify the work-bearing argument
    (the callable whose *body* runs off-loop). Every other argument expression is
    evaluated ON-loop (eager-evaluation rule).
    """

    dotted: str
    work_arg_index: int                 # positional index of the work callable
    work_arg_keyword: str = ""          # or a keyword name, if applicable
    extra_eager_args: tuple = ()        # indices that are call args to the work fn


@dataclass(frozen=True)
class ScheduleOnLoopSpec:
    """A primitive that schedules a callable/coroutine ON the event loop (D1/D8).

    Covers ``loop.call_soon`` (sync callable -> analyzed on-loop) and the
    coroutine schedulers (``asyncio.create_task`` etc.) and re-entry primitives
    (``anyio.from_thread.run``). ``work_arg_index`` is the scheduled callable.
    A re-entry spec wins over an enclosing offload context (precedence in taint).
    """

    dotted: str
    work_arg_index: int
    schedules_coroutine: bool = False   # True: arg is a coroutine (suppresses EVL004)
    is_reentry: bool = False            # re-sets on_loop even inside an offload


@dataclass(frozen=True)
class YieldingAwaitSpec:
    """An await target that genuinely yields to the loop (D8, for EVL003)."""

    dotted: str


# --- Framework shape-DSL (D2) -------------------------------------------------
# A function's framework *role* is decided by structural shape matched against
# these rows, never by a hardcoded verb set in logic.

@dataclass(frozen=True)
class FrameworkShape:
    """How a framework marks a callable as a threadpooled (off-loop) handler.

    kind:
      - "decorator-attr-call": @router.get(...) / @app.post(...) — decorator is a
        Call whose func is an Attribute whose attr is in ``attrs``.
      - "decorator-bare-call": @get(...) — decorator is a Call whose func is a
        Name in ``attrs`` (Litestar-style, receiver-less).
      - "dependency-callable": value passed to a Depends(...)-like marker named in
        ``attrs`` runs threadpooled if it is a plain ``def``.
    A handler/dependency declared ``def`` is OFF-loop; declared ``async def`` is
    ON-loop. Unresolved/ambiguous shape -> off-loop default (ADR-0008).
    """

    kind: str
    attrs: frozenset            # method/marker names that count (DATA, e.g. get/post)


@dataclass(frozen=True)
class WrapperSpec:
    """A callable-wrapping helper (e.g. ``functools.partial``). The target
    callable is at ``target_arg_index``; the remaining positional args are bound
    arguments evaluated ON-loop at wrap time (eager)."""

    dotted: str
    target_arg_index: int = 0


@dataclass
class Registry:
    blockers: dict = field(default_factory=dict)       # dotted -> BlockerSpec
    offloads: dict = field(default_factory=dict)       # dotted -> OffloadSpec
    schedulers: dict = field(default_factory=dict)     # dotted -> ScheduleOnLoopSpec
    yielding_awaits: set = field(default_factory=set)  # dotted strings
    framework_shapes: list = field(default_factory=list)  # FrameworkShape
    dependency_markers: frozenset = frozenset()        # Depends-like marker names
    wrappers: dict = field(default_factory=dict)       # dotted -> WrapperSpec

    def match_wrapper(self, dotted_target: str):
        spec = self.wrappers.get(dotted_target)
        if spec is not None:
            return spec
        for key, spec in self.wrappers.items():
            if self._suffix_match(dotted_target, key):
                return spec
        return None

    # ---- suffix matching helpers -------------------------------------------
    @staticmethod
    def _suffix_match(dotted_target: str, key: str) -> bool:
        if dotted_target == key:
            return True
        return dotted_target.endswith("." + key)

    def match_blocker(self, dotted_target: str) -> BlockerSpec | None:
        spec = self.blockers.get(dotted_target)
        if spec is not None:
            return spec
        for key, spec in self.blockers.items():
            if "." in key and self._suffix_match(dotted_target, key):
                return spec
        return None

    def match_offload(self, dotted_target: str) -> OffloadSpec | None:
        spec = self.offloads.get(dotted_target)
        if spec is not None:
            return spec
        for key, spec in self.offloads.items():
            if self._suffix_match(dotted_target, key):
                return spec
        return None

    def match_scheduler(self, dotted_target: str) -> ScheduleOnLoopSpec | None:
        spec = self.schedulers.get(dotted_target)
        if spec is not None:
            return spec
        for key, spec in self.schedulers.items():
            if self._suffix_match(dotted_target, key):
                return spec
        return None

    def is_yielding_await(self, dotted_target: str) -> bool:
        if dotted_target in self.yielding_awaits:
            return True
        return any(
            self._suffix_match(dotted_target, k) for k in self.yielding_awaits
        )


def default_registry() -> Registry:
    """Seed registry (the curated knowledge base). Extensible via config."""

    B = BlockerSpec
    blockers = {
        # --- sync sleep ------------------------------------------------------
        "time.sleep": B("time.sleep", Code.SYNC_IO, "time.sleep blocks the event loop",
                        "asyncio.sleep"),
        # --- sync HTTP -------------------------------------------------------
        "requests.get": B("requests.get", Code.SYNC_IO, "requests.get performs blocking HTTP",
                          "httpx.AsyncClient.get"),
        "requests.post": B("requests.post", Code.SYNC_IO, "requests.post performs blocking HTTP",
                           "httpx.AsyncClient.post"),
        "requests.put": B("requests.put", Code.SYNC_IO, "requests.put performs blocking HTTP",
                          "httpx.AsyncClient.put"),
        "requests.delete": B("requests.delete", Code.SYNC_IO, "requests.delete performs blocking HTTP",
                             "httpx.AsyncClient.delete"),
        "requests.patch": B("requests.patch", Code.SYNC_IO, "requests.patch performs blocking HTTP",
                            "httpx.AsyncClient.patch"),
        "requests.head": B("requests.head", Code.SYNC_IO, "requests.head performs blocking HTTP",
                           "httpx.AsyncClient.head"),
        "requests.request": B("requests.request", Code.SYNC_IO, "requests.request performs blocking HTTP",
                              "httpx.AsyncClient.request"),
        "requests.Session.get": B("requests.Session.get", Code.SYNC_IO,
                                  "requests Session performs blocking HTTP", "httpx.AsyncClient"),
        "urllib.request.urlopen": B("urllib.request.urlopen", Code.SYNC_IO,
                                    "urlopen performs blocking HTTP", "aiohttp / httpx"),
        # --- sync file I/O ---------------------------------------------------
        "open": B("open", Code.SYNC_IO, "open() performs blocking file I/O",
                  "anyio.open_file"),
        # --- sockets ---------------------------------------------------------
        "socket.socket.recv": B("socket.socket.recv", Code.SYNC_IO, "socket.recv blocks",
                                "asyncio streams"),
        "socket.socket.send": B("socket.socket.send", Code.SYNC_IO, "socket.send blocks",
                                "asyncio streams"),
        # --- subprocess ------------------------------------------------------
        "subprocess.run": B("subprocess.run", Code.SYNC_IO, "subprocess.run waits for a process",
                            "asyncio.create_subprocess_exec"),
        "subprocess.call": B("subprocess.call", Code.SYNC_IO, "subprocess.call waits for a process",
                             "asyncio.create_subprocess_exec"),
        "subprocess.check_output": B("subprocess.check_output", Code.SYNC_IO,
                                     "subprocess.check_output waits for a process",
                                     "asyncio.create_subprocess_exec"),
        "subprocess.Popen.wait": B("subprocess.Popen.wait", Code.SYNC_IO,
                                   "Popen.wait blocks", "asyncio subprocess"),
        "subprocess.Popen.communicate": B("subprocess.Popen.communicate", Code.SYNC_IO,
                                          "Popen.communicate blocks", "asyncio subprocess"),
        # --- sync DB drivers (EVL011) ---------------------------------------
        "psycopg2.connect": B("psycopg2.connect", Code.SYNC_DB_DRIVER,
                              "psycopg2 is a blocking driver", "asyncpg / psycopg (async)"),
        "sqlite3.Connection.execute": B("sqlite3.Connection.execute", Code.SYNC_DB_DRIVER,
                                        "sqlite3 execute blocks", "aiosqlite"),
        "sqlite3.Cursor.execute": B("sqlite3.Cursor.execute", Code.SYNC_DB_DRIVER,
                                    "sqlite3 cursor execute blocks", "aiosqlite"),
        "pymysql.connect": B("pymysql.connect", Code.SYNC_DB_DRIVER,
                             "pymysql is a blocking driver", "aiomysql / asyncmy"),
        # --- known-heavy CPU (EVL002) ---------------------------------------
        "bcrypt.hashpw": B("bcrypt.hashpw", Code.HEAVY_CPU, "bcrypt.hashpw is CPU-heavy",
                           "offload to a thread/process pool"),
        "bcrypt.checkpw": B("bcrypt.checkpw", Code.HEAVY_CPU, "bcrypt.checkpw is CPU-heavy",
                            "offload to a thread/process pool"),
        "hashlib.pbkdf2_hmac": B("hashlib.pbkdf2_hmac", Code.HEAVY_CPU,
                                 "pbkdf2_hmac is CPU-heavy", "offload to a pool"),
        "hashlib.scrypt": B("hashlib.scrypt", Code.HEAVY_CPU, "scrypt is CPU-heavy",
                            "offload to a pool"),
        "zlib.compress": B("zlib.compress", Code.HEAVY_CPU, "zlib.compress is CPU-heavy",
                           "offload to a pool"),
        "gzip.compress": B("gzip.compress", Code.HEAVY_CPU, "gzip.compress is CPU-heavy",
                           "offload to a pool"),
    }

    offloads = {
        # asyncio.to_thread(fn, *args) -> fn is arg 0
        "asyncio.to_thread": OffloadSpec("asyncio.to_thread", work_arg_index=0),
        # loop.run_in_executor(executor, fn, *args) -> fn is arg 1
        "run_in_executor": OffloadSpec("run_in_executor", work_arg_index=1),
        # anyio.to_thread.run_sync(fn, *args) -> fn is arg 0
        "anyio.to_thread.run_sync": OffloadSpec("anyio.to_thread.run_sync", work_arg_index=0),
        "to_thread.run_sync": OffloadSpec("to_thread.run_sync", work_arg_index=0),
        # starlette/fastapi run_in_threadpool(fn, *args) -> fn is arg 0
        "run_in_threadpool": OffloadSpec("run_in_threadpool", work_arg_index=0),
        # trio (registry-only support)
        "trio.to_thread.run_sync": OffloadSpec("trio.to_thread.run_sync", work_arg_index=0),
    }

    schedulers = {
        # call_soon family: sync callable scheduled ON loop -> analyze on-loop
        "call_soon": ScheduleOnLoopSpec("call_soon", work_arg_index=0),
        "call_later": ScheduleOnLoopSpec("call_later", work_arg_index=1),  # (delay, cb, ...)
        "call_at": ScheduleOnLoopSpec("call_at", work_arg_index=1),
        "call_soon_threadsafe": ScheduleOnLoopSpec("call_soon_threadsafe", work_arg_index=0),
        # coroutine schedulers: arg is a coroutine, stays on the same loop
        "asyncio.create_task": ScheduleOnLoopSpec("asyncio.create_task", 0, schedules_coroutine=True),
        "asyncio.ensure_future": ScheduleOnLoopSpec("asyncio.ensure_future", 0, schedules_coroutine=True),
        # re-entry: worker pushes work back ON the loop, wins over offload
        "anyio.from_thread.run": ScheduleOnLoopSpec("anyio.from_thread.run", 0, is_reentry=True),
        "from_thread.run": ScheduleOnLoopSpec("from_thread.run", 0, is_reentry=True),
        "asyncio.run_coroutine_threadsafe": ScheduleOnLoopSpec(
            "asyncio.run_coroutine_threadsafe", 0, schedules_coroutine=True, is_reentry=True),
    }

    yielding_awaits = {
        "asyncio.sleep",
        "anyio.sleep",
        "trio.sleep",
    }

    framework_shapes = [
        # FastAPI / Starlette: @router.get(...), @app.post(...), @router.api_route(...)
        FrameworkShape("decorator-attr-call", frozenset({
            "get", "post", "put", "delete", "patch", "head", "options", "trace",
            "api_route", "websocket", "route",
        })),
        # Litestar-style receiver-less: @get(...), @post(...)
        FrameworkShape("decorator-bare-call", frozenset({
            "get", "post", "put", "delete", "patch", "head",
        })),
    ]

    dependency_markers = frozenset({"Depends", "Security"})

    wrappers = {
        "functools.partial": WrapperSpec("functools.partial", target_arg_index=0),
        "partial": WrapperSpec("partial", target_arg_index=0),
    }

    return Registry(
        blockers=blockers,
        offloads=offloads,
        schedulers=schedulers,
        yielding_awaits=yielding_awaits,
        framework_shapes=framework_shapes,
        dependency_markers=dependency_markers,
        wrappers=wrappers,
    )
