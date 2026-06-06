"""Adversarial benchmark cases discovered by the breaker loop (ADR-0004 ratchet).

Each test is a realistic escape the breaker found. Once added, these must never
regress. Sibling variants enforce generic fixes (ADR-0005).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from evloop_lint.codes import Confidence  # noqa: E402
from harness import find_all, has, run_project  # noqa: E402


# === FALSE POSITIVES (must NOT flag) =======================================

def test_fp_offloaded_lambda_body_not_flagged():
    # blocker inside an offloaded inline lambda runs off-loop -> safe
    r = run_project({"app/main.py": (
        "import asyncio, requests\n"
        "async def fetch(path):\n"
        "    loop = asyncio.get_running_loop()\n"
        "    return await loop.run_in_executor(None, lambda: requests.get(path).json())\n"
    )})
    assert not has(r, "EVL001"), [f.location() for f in find_all(r, "EVL001")]


def test_fp_offloaded_lambda_to_thread_sibling():
    # sibling: asyncio.to_thread + open()
    r = run_project({"app/main.py": (
        "import asyncio\n"
        "async def h(p):\n"
        "    return await asyncio.to_thread(lambda: open(p).read())\n"
    )})
    assert not has(r, "EVL001")


def test_fp_while_true_awaits_project_coroutine():
    # while True awaiting a genuine project coroutine yields the loop -> safe
    r = run_project({
        "app/streaming.py": (
            "import asyncio\n"
            "async def next_tick(queue):\n"
            "    return await queue.get()\n"
        ),
        "app/main.py": (
            "from app.streaming import next_tick\n"
            "async def feed(ws, queue):\n"
            "    while True:\n"
            "        item = await next_tick(queue)\n"
            "        await ws.send_json(item)\n"
        ),
    }, confidence=Confidence.POSSIBLE)
    assert not has(r, "EVL003"), [f.location() for f in find_all(r, "EVL003")]


def test_fp_while_true_awaits_method_sibling():
    # sibling: awaiting a method call (queue.get) directly
    r = run_project({"app/main.py": (
        "async def feed(ws):\n"
        "    while True:\n"
        "        msg = await ws.receive_text()\n"
        "        await ws.send_text(msg)\n"
    )}, confidence=Confidence.POSSIBLE)
    assert not has(r, "EVL003")


def test_fp_method_name_collides_with_module_coroutine():
    # Found in the wild (langflow): a SYNC method `obj.attr.update_settings(...)`
    # shares its bare name with a module-level `async def update_settings`. The
    # bare-suffix resolver must NOT treat the multi-segment attribute call as a
    # call to the coroutine -> no spurious EVL004 "never awaited".
    r = run_project({"app/util.py": (
        "async def update_settings(cache=None):\n"
        "    svc = get_settings_service()\n"
        "    svc.settings.update_settings(cache=cache)\n"   # sync method, same name
    )})
    assert not has(r, "EVL004"), [f.location() for f in find_all(r, "EVL004")]


def test_fp_method_name_collides_with_module_function_sibling():
    # sibling (EVL001 variant): a sync method `self.db.query(...)` collides with a
    # module-level helper `query()` that itself reaches a blocker. The instance
    # method call must not be resolved to the module function.
    r = run_project({
        "app/db.py": (
            "import time\n"
            "def query():\n"
            "    time.sleep(1)\n"               # module-level blocker
        ),
        "app/main.py": (
            "async def handler(self):\n"
            "    self.repo.query()\n"           # instance method, same bare name
        ),
    })
    assert not has(r, "EVL001"), [f.location() for f in find_all(r, "EVL001")]


def test_fn_module_function_still_resolves_after_collision_guard():
    # Guard against over-correction: a genuine cross-root module-function call
    # (`from app.service import fetch; fetch()`) MUST still resolve and flag.
    r = run_project({
        "app/service.py": (
            "import time\n"
            "def fetch():\n"
            "    time.sleep(1)\n"
        ),
        "app/main.py": (
            "from app.service import fetch\n"
            "async def handler():\n"
            "    fetch()\n"
        ),
    })
    assert has(r, "EVL001"), "bare imported module-function call must still resolve"


# === FALSE NEGATIVES (must flag) ===========================================

def test_fn_constructor_init_blocker():
    # blocker in __init__ of a class constructed on-loop (receiver of offload)
    r = run_project({
        "app/report.py": (
            "import requests\n"
            "class ReportBuilder:\n"
            "    def __init__(self, url):\n"
            "        self.raw = requests.get(url).json()\n"
            "    def render(self):\n"
            "        return self.raw\n"
        ),
        "app/main.py": (
            "import asyncio\n"
            "from app.report import ReportBuilder\n"
            "async def build(url):\n"
            "    return await asyncio.to_thread(ReportBuilder(url).render)\n"
        ),
    })
    assert has(r, "EVL001"), "blocker in on-loop constructor __init__ must be flagged"


def test_fn_constructor_init_bcrypt_sibling():
    # sibling: run_in_executor + bcrypt heavy CPU in __init__
    r = run_project({
        "app/hasher.py": (
            "import bcrypt\n"
            "class Hasher:\n"
            "    def __init__(self, pw, salt):\n"
            "        self.digest = bcrypt.hashpw(pw, salt)\n"
            "    def hex(self):\n"
            "        return self.digest.hex()\n"
        ),
        "app/main.py": (
            "import asyncio\n"
            "from app.hasher import Hasher\n"
            "async def make_hash(pw, salt):\n"
            "    loop = asyncio.get_event_loop()\n"
            "    return await loop.run_in_executor(None, Hasher(pw, salt).hex)\n"
        ),
    })
    assert has(r, "EVL002")


def test_fn_constructor_called_directly_on_loop():
    # the plain on-loop construction (no offload) must also flag
    r = run_project({
        "app/report.py": (
            "import time\n"
            "class Builder:\n"
            "    def __init__(self):\n"
            "        time.sleep(1)\n"
        ),
        "app/main.py": (
            "from app.report import Builder\n"
            "async def h():\n"
            "    b = Builder()\n"
        ),
    })
    assert has(r, "EVL001")


def test_fn_bound_method_scheduled_callback():
    # self.x.method passed to call_later (scheduled on-loop) must be resolved
    r = run_project({
        "app/cache.py": (
            "import requests\n"
            "class CacheManager:\n"
            "    def __init__(self, url):\n"
            "        self.url = url\n"
            "    def reload(self):\n"
            "        requests.get(self.url)\n"
        ),
        "app/sched.py": (
            "class RefreshScheduler:\n"
            "    def __init__(self, mgr):\n"
            "        self._cache = mgr\n"
            "    def schedule(self, loop):\n"
            "        loop.call_later(0, self._cache.reload)\n"
        ),
        "app/main.py": (
            "import asyncio\n"
            "from app.cache import CacheManager\n"
            "from app.sched import RefreshScheduler\n"
            "_m = CacheManager('http://x')\n"
            "_s = RefreshScheduler(_m)\n"
            "async def refresh():\n"
            "    loop = asyncio.get_running_loop()\n"
            "    _s.schedule(loop)\n"
        ),
    }, confidence=Confidence.PROBABLE)
    # Reached via two Tier-3 method resolutions (_s.schedule, self._cache.reload)
    # so this is a 'probable'-tier finding (opt-in), per the confidence design.
    assert has(r, "EVL001"), "bound-method callback scheduled on-loop must be flagged"


def test_fn_partial_wrapped_scheduled_callback():
    # loop.call_soon(partial(fn, arg)) -> fn runs on-loop
    r = run_project({
        "app/tasks.py": (
            "import time\n"
            "def flush(retries):\n"
            "    time.sleep(retries)\n"
        ),
        "app/main.py": (
            "import asyncio\n"
            "from functools import partial\n"
            "from app.tasks import flush\n"
            "async def sched():\n"
            "    loop = asyncio.get_event_loop()\n"
            "    loop.call_soon(partial(flush, 5))\n"
        ),
    })
    assert has(r, "EVL001"), "partial-wrapped scheduled callback must be flagged"


def test_fn_property_backed_blocker():
    # attribute access of a @property whose getter blocks
    r = run_project({
        "app/config.py": (
            "class Settings:\n"
            "    @property\n"
            "    def dsn(self):\n"
            "        return open('/etc/secrets').read()\n"
        ),
        "app/main.py": (
            "from app.config import Settings\n"
            "settings = Settings()\n"
            "async def status():\n"
            "    return settings.dsn\n"
        ),
    }, confidence=Confidence.PROBABLE)
    # property access is a heuristic (name-based) getter match -> probable tier.
    assert has(r, "EVL001"), "@property getter blocker must be flagged"


def test_fn_async_cm_aenter_blocker():
    # async with over a CM whose async __aenter__ body blocks
    r = run_project({
        "app/pool.py": (
            "import psycopg2\n"
            "class Conn:\n"
            "    def __init__(self, dsn):\n"
            "        self.dsn = dsn\n"
            "    async def __aenter__(self):\n"
            "        self.c = psycopg2.connect(self.dsn)\n"
            "        return self.c\n"
            "    async def __aexit__(self, *a):\n"
            "        pass\n"
        ),
        "app/main.py": (
            "from app.pool import Conn\n"
            "async def summary():\n"
            "    async with Conn('dsn') as c:\n"
            "        return 1\n"
        ),
    })
    assert has(r, "EVL011") or has(r, "EVL001"), "blocking __aenter__ must be flagged"
