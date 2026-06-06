"""Detection benchmark: must_flag / must_not_flag cases covering D1–D9.

This is the seed of the permanent labeled benchmark (ADR-0004). Each test names
the mechanism it exercises; sibling variants live alongside to enforce generic
detection (ADR-0005).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from evloop_lint.codes import Confidence  # noqa: E402
from harness import codes, find_all, has, run_project  # noqa: E402


# --------------------------------------------------------------------------
# Core: direct blocking calls in an async def (must_flag)
# --------------------------------------------------------------------------
def test_direct_sleep_flagged():
    r = run_project({"app/main.py": (
        "import time\n"
        "async def h():\n"
        "    time.sleep(1)\n"
    )})
    assert has(r, "EVL001", line=3)


def test_direct_requests_flagged():
    r = run_project({"app/main.py": (
        "import requests\n"
        "async def h():\n"
        "    requests.get('http://x')\n"
    )})
    assert has(r, "EVL001", line=3)


def test_aliased_import_flagged():
    # sibling variant: same mechanism via import alias
    r = run_project({"app/main.py": (
        "import time as t\n"
        "async def h():\n"
        "    t.sleep(1)\n"
    )})
    assert has(r, "EVL001", line=3)


def test_from_import_flagged():
    # sibling variant: from time import sleep
    r = run_project({"app/main.py": (
        "from time import sleep\n"
        "async def h():\n"
        "    sleep(1)\n"
    )})
    assert has(r, "EVL001", line=3)


# --------------------------------------------------------------------------
# Nesting + cross-file (the differentiator)
# --------------------------------------------------------------------------
def test_nested_cross_file_flagged():
    r = run_project({
        "app/db.py": "import time\ndef query():\n    time.sleep(1)\n",
        "app/svc.py": "from app.db import query\ndef fetch():\n    return query()\n",
        "app/main.py": (
            "from app.svc import fetch\n"
            "async def h():\n"
            "    fetch()\n"
        ),
    })
    assert has(r, "EVL001")
    f = find_all(r, "EVL001")[0]
    # chain should be entry -> fetch -> query -> time.sleep
    labels = [s.label for s in f.chain]
    assert "async entry" in labels and "blocks via" in labels
    assert len(f.chain) >= 4


def test_depth_limit_respected():
    # chain of 6 hops, default max-depth 4 -> not a definite finding
    files = {"app/main.py": (
        "from app.l1 import l1\nasync def h():\n    l1()\n")}
    for i in range(1, 6):
        nxt = f"from app.l{i+1} import l{i+1}\n" if i < 5 else "import time\n"
        body = f"    l{i+1}()\n" if i < 5 else "    time.sleep(1)\n"
        files[f"app/l{i}.py"] = f"{nxt}def l{i}():\n{body}"
    r = run_project(files, max_depth=4)
    # definite should be silent; truncation surfaced at possible tier
    assert not has(r, "EVL001")
    r2 = run_project(files, max_depth=4, confidence=Confidence.POSSIBLE)
    assert has(r2, "EVL005")  # depth truncation surfaced


def test_raising_depth_finds_deep_blocker():
    files = {"app/main.py": (
        "from app.l1 import l1\nasync def h():\n    l1()\n")}
    for i in range(1, 6):
        nxt = f"from app.l{i+1} import l{i+1}\n" if i < 5 else "import time\n"
        body = f"    l{i+1}()\n" if i < 5 else "    time.sleep(1)\n"
        files[f"app/l{i}.py"] = f"{nxt}def l{i}():\n{body}"
    r = run_project(files, max_depth=10)
    assert has(r, "EVL001")


# --------------------------------------------------------------------------
# Offload escape hatches (must_not_flag) — D1
# --------------------------------------------------------------------------
def test_asyncio_to_thread_safe():
    r = run_project({"app/main.py": (
        "import asyncio, time\n"
        "async def h():\n"
        "    await asyncio.to_thread(time.sleep, 1)\n"
    )})
    assert not has(r, "EVL001")


def test_run_in_executor_safe():
    r = run_project({"app/main.py": (
        "import asyncio, time\n"
        "async def h():\n"
        "    loop = asyncio.get_event_loop()\n"
        "    await loop.run_in_executor(None, time.sleep, 1)\n"
    )})
    assert not has(r, "EVL001")


def test_offloaded_helper_subtree_safe():
    # offload a helper whose body contains the blocker -> safe
    r = run_project({
        "app/work.py": "import time\ndef work():\n    time.sleep(1)\n",
        "app/main.py": (
            "import asyncio\n"
            "from app.work import work\n"
            "async def h():\n"
            "    await asyncio.to_thread(work)\n"
        ),
    })
    assert not has(r, "EVL001")


def test_inverse_offload_trap_flagged():
    # offload one call, a sibling blocker right after still blocks
    r = run_project({"app/main.py": (
        "import asyncio, time\n"
        "async def h():\n"
        "    await asyncio.to_thread(time.sleep, 1)\n"
        "    time.sleep(2)\n"  # line 4 -> still on-loop, must flag
    )})
    assert has(r, "EVL001", line=4)
    assert not has(r, "EVL001", line=3)


def test_run_in_threadpool_safe():
    # starlette/fastapi helper
    r = run_project({
        "app/work.py": "import time\ndef work():\n    time.sleep(1)\n",
        "app/main.py": (
            "from starlette.concurrency import run_in_threadpool\n"
            "from app.work import work\n"
            "async def h():\n"
            "    await run_in_threadpool(work)\n"
        ),
    })
    assert not has(r, "EVL001")


# --------------------------------------------------------------------------
# Genuinely async (must_not_flag)
# --------------------------------------------------------------------------
def test_asyncio_sleep_safe():
    r = run_project({"app/main.py": (
        "import asyncio\nasync def h():\n    await asyncio.sleep(1)\n"
    )})
    assert not has(r, "EVL001")


# --------------------------------------------------------------------------
# Entry points (D2): plain def endpoint threadpooled (must_not_flag)
# --------------------------------------------------------------------------
def test_sync_def_endpoint_not_entry():
    r = run_project({"app/main.py": (
        "import time\n"
        "app = object()\n"
        "@app.get('/x')\n"
        "def endpoint():\n"
        "    time.sleep(1)\n"  # plain def -> threadpooled -> safe
    )})
    assert not has(r, "EVL001")


def test_async_def_endpoint_is_entry():
    r = run_project({"app/main.py": (
        "import time\n"
        "app = object()\n"
        "@app.get('/x')\n"
        "async def endpoint():\n"
        "    time.sleep(1)\n"
    )})
    assert has(r, "EVL001", line=5)


def test_plain_def_blocker_reached_directly_flagged():
    # dual-context recall recovery: a plain def reached DIRECTLY from async is on-loop
    r = run_project({
        "app/helpers.py": "import time\ndef helper():\n    time.sleep(1)\n",
        "app/main.py": (
            "from app.helpers import helper\n"
            "async def h():\n"
            "    helper()\n"
        ),
    })
    assert has(r, "EVL001")


# --------------------------------------------------------------------------
# Schedulers / call_soon family (D8)
# --------------------------------------------------------------------------
def test_call_soon_sync_blocker_flagged():
    r = run_project({
        "app/cb.py": "import time\ndef cb():\n    time.sleep(1)\n",
        "app/main.py": (
            "import asyncio\n"
            "from app.cb import cb\n"
            "async def h():\n"
            "    loop = asyncio.get_event_loop()\n"
            "    loop.call_soon(cb)\n"
        ),
    })
    assert has(r, "EVL001")


# --------------------------------------------------------------------------
# await misuse (D8/D9) EVL004
# --------------------------------------------------------------------------
def test_unawaited_coroutine_flagged():
    r = run_project({"app/main.py": (
        "async def inner():\n"
        "    return 1\n"
        "async def h():\n"
        "    inner()\n"  # discarded coroutine
    )})
    assert has(r, "EVL004", line=4)


def test_create_task_not_misuse():
    r = run_project({"app/main.py": (
        "import asyncio\n"
        "async def inner():\n"
        "    return 1\n"
        "async def h():\n"
        "    asyncio.create_task(inner())\n"
    )})
    assert not has(r, "EVL004")


# --------------------------------------------------------------------------
# Heavy CPU (D8) EVL002
# --------------------------------------------------------------------------
def test_bcrypt_flagged_heavy_cpu():
    r = run_project({"app/main.py": (
        "import bcrypt\n"
        "async def h(pw, salt):\n"
        "    bcrypt.hashpw(pw, salt)\n"
    )})
    assert has(r, "EVL002", line=3)


# --------------------------------------------------------------------------
# Structural unbounded (D8) EVL003 — possible tier only
# --------------------------------------------------------------------------
def test_while_true_no_yield_possible():
    r = run_project({"app/main.py": (
        "async def h():\n"
        "    while True:\n"
        "        x = 1\n"
    )}, confidence=Confidence.POSSIBLE)
    assert has(r, "EVL003")


def test_while_true_with_yield_safe():
    r = run_project({"app/main.py": (
        "import asyncio\n"
        "async def h(q):\n"
        "    while True:\n"
        "        await asyncio.sleep(0)\n"
    )}, confidence=Confidence.POSSIBLE)
    assert not has(r, "EVL003")


def test_evl003_not_in_definite_default():
    r = run_project({"app/main.py": (
        "async def h():\n"
        "    while True:\n"
        "        x = 1\n"
    )})
    assert not has(r, "EVL003")


# --------------------------------------------------------------------------
# Suppression (D6)
# --------------------------------------------------------------------------
def test_noqa_blanket_suppresses():
    r = run_project({"app/main.py": (
        "import time\n"
        "async def h():\n"
        "    time.sleep(1)  # noqa\n"
    )})
    assert not has(r, "EVL001")


def test_noqa_specific_code_suppresses():
    r = run_project({"app/main.py": (
        "import time\n"
        "async def h():\n"
        "    time.sleep(1)  # noqa: EVL001\n"
    )})
    assert not has(r, "EVL001")


def test_noqa_wrong_code_does_not_suppress():
    r = run_project({"app/main.py": (
        "import time\n"
        "async def h():\n"
        "    time.sleep(1)  # noqa: EVL999\n"
    )})
    assert has(r, "EVL001")


# --------------------------------------------------------------------------
# Optimism (ADR-0003): unresolved is safe
# --------------------------------------------------------------------------
def test_unresolved_call_is_safe():
    r = run_project({"app/main.py": (
        "async def h(svc):\n"
        "    svc.process()\n"  # unknown type -> safe
    )})
    assert not has(r, "EVL001")


def test_multiple_same_primitive_distinct_sites():
    # two distinct time.sleep occurrences in one file must BOTH be reported
    r = run_project({"app/main.py": (
        "import time\n"
        "async def h():\n"
        "    time.sleep(1)\n"
        "    time.sleep(2)\n"
    )})
    sleeps = find_all(r, "EVL001")
    assert len(sleeps) == 2
    assert {f.lineno for f in sleeps} == {3, 4}


def test_blocker_in_try_and_for_bodies():
    r = run_project({"app/main.py": (
        "import time\n"
        "async def h(items):\n"
        "    try:\n"
        "        time.sleep(1)\n"
        "    except Exception:\n"
        "        pass\n"
        "    for i in items:\n"
        "        time.sleep(2)\n"
    )})
    lines = {f.lineno for f in find_all(r, "EVL001")}
    assert lines == {4, 8}


def test_third_party_unknown_safe():
    r = run_project({"app/main.py": (
        "import some_unknown_lib\n"
        "async def h():\n"
        "    some_unknown_lib.do_thing()\n"
    )})
    assert not has(r, "EVL001")
