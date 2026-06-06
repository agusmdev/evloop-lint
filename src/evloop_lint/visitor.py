"""AST -> IR extraction (D3).

One pass per file. Emits raw structural facts; no registry knowledge is applied
here. ``# noqa`` directives are harvested via ``tokenize`` so they align with the
same line numbering used by suppression (range-aware in :mod:`suppress`).
"""

from __future__ import annotations

import ast
import io
import re
import tokenize

from .ir import (
    AttrAccessIR,
    CallableRef,
    CallIR,
    ClassIR,
    DecoratorShape,
    FunctionIR,
    ImportIR,
    LoopIR,
    ModuleIR,
)

_NOQA_RE = re.compile(r"#\s*noqa(?::\s*(?P<codes>[A-Z0-9, ]+))?", re.IGNORECASE)


def _dotted_chain(node: ast.AST) -> tuple:
    """Return the dotted attribute/name chain of an expression, or () if dynamic.

    ``a.b.c`` -> ("a", "b", "c"); ``a`` -> ("a",); ``a().b`` -> () (call result).
    """
    parts = []
    cur = node
    while True:
        if isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        elif isinstance(cur, ast.Name):
            parts.append(cur.id)
            break
        else:
            return ()
    parts.reverse()
    return tuple(parts)


def _callable_ref(node: ast.expr) -> CallableRef:
    """Build a CallableRef from an argument expression (for offload/schedule)."""
    chain = _dotted_chain(node)
    if chain:
        return CallableRef(name_chain=chain)
    if isinstance(node, ast.Lambda):
        inner = []
        for sub in ast.walk(node.body):
            if isinstance(sub, ast.Call):
                inner.append(_call_ir(sub, awaited=False, bare=False))
        return CallableRef(is_lambda=True, lambda_calls=inner)
    if isinstance(node, ast.Call):
        # A wrapper call like functools.partial(fn, ...). Record the wrapper's
        # func chain + positional arg refs so the resolver can unwrap it via the
        # wrapper registry (the target callable is one of these args).
        wchain = _dotted_chain(node.func)
        wargs = [_callable_ref(a) for a in node.args if not isinstance(a, ast.Starred)]
        return CallableRef(is_unresolvable=True, wrapper_chain=wchain,
                           wrapper_arg_refs=wargs)
    return CallableRef(is_unresolvable=True)


def _walk_no_lambda(node: ast.AST):
    """Like ast.walk, but does not descend into ast.Lambda bodies (their calls
    are deferred work, not eager on-loop evaluation)."""
    from collections import deque
    todo = deque([node])
    while todo:
        cur = todo.popleft()
        yield cur
        for child in ast.iter_child_nodes(cur):
            if isinstance(child, ast.Lambda):
                continue  # do not descend into deferred lambda bodies
            todo.append(child)


def _call_ir(node: ast.Call, *, awaited: bool, bare: bool) -> CallIR:
    func_chain = _dotted_chain(node.func)
    callee_is_call_result = func_chain == () and isinstance(node.func, ast.Attribute)
    # Positional argument callable refs (offload/schedule resolution).
    arg_refs = [_callable_ref(a) for a in node.args if not isinstance(a, ast.Starred)]
    # Eager nested calls inside the argument list: any Call appearing directly as
    # an arg or nested in arg expressions runs on-loop before dispatch (D1).
    # NB: do NOT descend into a lambda body — a lambda passed as an argument is
    # deferred/off-loop work (captured via arg_refs); calls inside it run wherever
    # the lambda is later invoked, not eagerly on-loop.
    eager_calls = []
    for a in list(node.args) + [kw.value for kw in node.keywords]:
        if isinstance(a, ast.Lambda):
            continue  # the whole arg is deferred work; nothing here is eager
        for sub in _walk_no_lambda(a):
            if isinstance(sub, ast.Call):
                eager_calls.append(_call_ir(sub, awaited=False, bare=False))
    kw_names = tuple(kw.arg for kw in node.keywords if kw.arg)
    return CallIR(
        func_chain=func_chain,
        lineno=node.lineno,
        col=node.col_offset,
        end_lineno=getattr(node, "end_lineno", node.lineno) or node.lineno,
        is_awaited=awaited,
        is_bare_expr=bare,
        arg_refs=arg_refs,
        keyword_arg_names=kw_names,
        eager_arg_calls=eager_calls,
        callee_is_call_result=callee_is_call_result,
    )


def _decorator_shape(node: ast.expr) -> DecoratorShape:
    if isinstance(node, ast.Call):
        inner = node.func
        if isinstance(inner, ast.Attribute):
            return DecoratorShape(
                kind="attr-call",
                receiver_chain=_dotted_chain(inner.value),
                attr=inner.attr,
                is_call=True,
                lineno=node.lineno,
            )
        if isinstance(inner, ast.Name):
            return DecoratorShape(kind="bare-call", attr=inner.id, is_call=True, lineno=node.lineno)
        return DecoratorShape(kind="other", lineno=node.lineno)
    if isinstance(node, ast.Attribute):
        return DecoratorShape(kind="attr", receiver_chain=_dotted_chain(node.value),
                              attr=node.attr, lineno=node.lineno)
    if isinstance(node, ast.Name):
        return DecoratorShape(kind="name", attr=node.id, lineno=node.lineno)
    return DecoratorShape(kind="other", lineno=getattr(node, "lineno", 0))


class _Visitor(ast.NodeVisitor):
    def __init__(self, module: str, path: str):
        self.module = module
        self.path = path
        self.functions: list = []
        self.classes: list = []
        self.imports: list = []
        self._scope: list = []          # qualname segments
        self._class_stack: list = []     # bare class names

    # ---- imports -----------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local = alias.asname or alias.name.split(".")[0]
            target = alias.name if alias.asname else alias.name.split(".")[0]
            # `import a.b.c` binds `a`; `import a.b.c as x` binds x->a.b.c
            target = alias.name if alias.asname else alias.name.split(".")[0]
            self.imports.append(ImportIR(local, target, is_module=True))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        mod = node.module or ""
        for alias in node.names:
            if alias.name == "*":
                continue
            local = alias.asname or alias.name
            dotted = f"{mod}.{alias.name}" if mod else alias.name
            self.imports.append(ImportIR(local, dotted, is_module=False))
        self.generic_visit(node)

    # ---- classes -----------------------------------------------------------
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        bases = tuple(_dotted_chain(b) for b in node.bases)
        method_names = tuple(
            n.name for n in node.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        qual = ".".join([self.module] + self._scope + [node.name])
        self.classes.append(ClassIR(node.name, qual, bases, method_names))
        self._scope.append(node.name)
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()
        self._scope.pop()

    # ---- functions ---------------------------------------------------------
    def visit_FunctionDef(self, node):
        self._handle_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node):
        self._handle_function(node, is_async=True)

    def _handle_function(self, node, *, is_async: bool):
        qual = ".".join([self.module] + self._scope + [node.name])
        enclosing_class = self._class_stack[-1] if self._class_stack else ""
        params = tuple(a.arg for a in node.args.args)
        fir = FunctionIR(
            qualname=qual,
            name=node.name,
            is_async=is_async,
            lineno=node.lineno,
            col=node.col_offset,
            end_lineno=getattr(node, "end_lineno", node.lineno) or node.lineno,
            decorators=[_decorator_shape(d) for d in node.decorator_list],
            enclosing_class=enclosing_class,
            is_method=bool(enclosing_class),
            param_names=params,
        )
        # Collect calls and loops within THIS function body, not descending into
        # nested function defs (they get their own FunctionIR).
        self._collect_body(node, fir)
        self.functions.append(fir)

        # Recurse into nested defs/classes for their own IR.
        self._scope.append(node.name)
        for child in node.body:
            self._visit_nested(child)
        self._scope.pop()

    def _visit_nested(self, node):
        if isinstance(node, ast.Import):
            self.visit_Import(node)
        elif isinstance(node, ast.ImportFrom):
            self.visit_ImportFrom(node)
        elif isinstance(node, ast.ClassDef):
            self.visit_ClassDef(node)
        elif isinstance(node, ast.FunctionDef):
            self._handle_function(node, is_async=False)
        elif isinstance(node, ast.AsyncFunctionDef):
            self._handle_function(node, is_async=True)
        else:
            for child in ast.iter_child_nodes(node):
                self._visit_nested(child)

    def _collect_body(self, func_node, fir: FunctionIR):
        """Walk the function body collecting calls + loops, but not entering
        nested function definitions."""
        for stmt in func_node.body:
            self._collect_stmt(stmt, fir)

    def _collect_stmt(self, node, fir: FunctionIR):
        # Do not descend into nested function/class definitions.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return

        # Loops -> LoopIR (EVL003)
        if isinstance(node, (ast.While, ast.For, ast.AsyncFor)):
            self._collect_loop(node, fir)

        # Bare expression statement: a discarded call value (EVL004 candidate)
        if isinstance(node, ast.Expr):
            self._collect_expr(node.value, fir, bare=True)
            # Still descend for nested calls inside (handled within _collect_expr)
        elif isinstance(node, ast.Await):
            self._collect_expr(node, fir, bare=False)
        else:
            # Generic descent through this statement's children expressions.
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                if isinstance(child, ast.stmt):
                    self._collect_stmt(child, fir)
                else:
                    self._collect_expr(child, fir, bare=False)

    def _collect_loop(self, node, fir: FunctionIR):
        is_unbounded = False
        if isinstance(node, ast.While):
            t = node.test
            if (isinstance(t, ast.Constant) and bool(t.value)) or (
                isinstance(t, ast.Name) and t.id == "True"
            ):
                is_unbounded = True
        # for-loops are treated as bounded unless iterating an unknown-length
        # producer; we conservatively treat plain for as bounded (optimistic).
        await_targets = []
        body_calls = []
        has_yield = False
        for sub in ast.walk(node):
            # ANY await (or async-for / async-with) is a genuine yield point: it
            # transfers control back to the event loop. We do not require the
            # awaited target to be a known sleep — awaiting a project coroutine,
            # queue.get, websocket.receive, an async iterator, etc. all yield.
            if isinstance(sub, ast.Await):
                has_yield = True
                if isinstance(sub.value, ast.Call):
                    ch = _dotted_chain(sub.value.func)
                    if ch:
                        await_targets.append(ch)
            elif isinstance(sub, (ast.AsyncFor, ast.AsyncWith)):
                has_yield = True
            elif isinstance(sub, ast.Call):
                ch = _dotted_chain(sub.func)
                if ch:
                    body_calls.append(ch)
        fir.loops.append(LoopIR(
            lineno=node.lineno, col=node.col_offset,
            end_lineno=getattr(node, "end_lineno", node.lineno) or node.lineno,
            is_unbounded=is_unbounded, has_yielding_await=has_yield,
            await_targets=tuple(await_targets), body_calls=tuple(body_calls),
        ))

    def _collect_expr(self, node, fir: FunctionIR, *, bare: bool):
        if isinstance(node, ast.Await):
            val = node.value
            if isinstance(val, ast.Call):
                fir.calls.append(_call_ir(val, awaited=True, bare=False))
                # Also descend into the awaited call's receiver + args so that a
                # blocking sub-call (e.g. requests.get(x) in `await f(requests.get(x))`
                # or a receiver chain) is not lost. Lambdas are deferred (skipped).
                self._descend_call_parts(val, fir)
                return
            for child in ast.iter_child_nodes(node):
                self._collect_expr(child, fir, bare=False)
            return
        if isinstance(node, ast.Call):
            fir.calls.append(_call_ir(node, awaited=False, bare=bare))
            # Method chaining / call-on-call-result: `requests.get(x).json()` must
            # still surface the inner `requests.get(x)`. Descend into the callee's
            # receiver expression and the argument expressions (all run on-loop in
            # this enclosing context). Lambda bodies are deferred work -> skipped.
            self._descend_call_parts(node, fir)
            return
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            # A bare attribute LOAD: a candidate @property getter invocation.
            recv = _dotted_chain(node.value)
            if recv:
                fir.attr_loads.append(AttrAccessIR(
                    attr=node.attr, receiver_chain=recv,
                    lineno=node.lineno, col=node.col_offset,
                    end_lineno=getattr(node, "end_lineno", node.lineno) or node.lineno,
                ))
            # descend into the receiver in case of chained property access
            self._collect_expr(node.value, fir, bare=False)
            return
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            self._collect_expr(child, fir, bare=False)

    def _descend_call_parts(self, call_node: ast.Call, fir: FunctionIR):
        """Descend into a call's receiver chain + argument expressions to collect
        nested calls (receiver calls, args), skipping deferred lambda bodies."""
        # Receiver of an attribute callee: `requests.get(x).json()` -> the func of
        # the outer call is `<requests.get(x)>.json`, whose .value is the inner call.
        func = call_node.func
        if isinstance(func, ast.Attribute):
            self._collect_expr(func.value, fir, bare=False)
        # Arguments (skip lambdas: deferred work, handled via arg_refs).
        for a in list(call_node.args) + [kw.value for kw in call_node.keywords]:
            if isinstance(a, ast.Lambda):
                continue
            self._collect_expr(a, fir, bare=False)


def _harvest_noqa(source: str) -> dict:
    """Map lineno -> set of suppressed codes ('' sentinel = blanket noqa)."""
    out: dict = {}
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in tokens:
            if tok.type == tokenize.COMMENT:
                m = _NOQA_RE.search(tok.string)
                if not m:
                    continue
                line = tok.start[0]
                codes_raw = m.group("codes")
                if codes_raw:
                    codes = {c.strip().upper() for c in codes_raw.split(",") if c.strip()}
                else:
                    codes = {""}  # blanket
                out.setdefault(line, set()).update(codes)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return out


def build_module_ir(source: str, module: str, path: str) -> ModuleIR:
    """Parse ``source`` and return its IR. On SyntaxError, returns a ModuleIR
    with ``parse_error`` set (EVL000)."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return ModuleIR(module=module, path=path, parse_error=str(exc))

    v = _Visitor(module, path)
    for node in tree.body:
        if isinstance(node, ast.Import):
            v.visit_Import(node)
        elif isinstance(node, ast.ImportFrom):
            v.visit_ImportFrom(node)
        elif isinstance(node, ast.ClassDef):
            v.visit_ClassDef(node)
        elif isinstance(node, ast.FunctionDef):
            v._handle_function(node, is_async=False)
        elif isinstance(node, ast.AsyncFunctionDef):
            v._handle_function(node, is_async=True)
        else:
            # Top-level statements may contain imports / nested defs (e.g. inside
            # `if TYPE_CHECKING:` or `try/except`); descend for those.
            v._visit_nested(node)

    return ModuleIR(
        module=module,
        path=path,
        imports=v.imports,
        functions=v.functions,
        classes=v.classes,
        noqa_lines=_harvest_noqa(source),
    )
