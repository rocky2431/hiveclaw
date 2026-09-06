"""Static, expiring authorization manifest for every RLS bypass callsite.

The allowlist records each enclosing function, reason, and coarse model access.
A companion AST fingerprint covers complete direct scopes and discoverable
contextmanager consumers, so predicate or write widening requires review.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RLSBypassCallsite:
    file: str
    function: str
    reason_expression: str
    allowed_query_fields: tuple[str, ...]

    @property
    def signature(self) -> tuple[str, str, str, tuple[str, ...]]:
        return (self.file, self.function, self.reason_expression, self.allowed_query_fields)


@dataclass(frozen=True, slots=True)
class RLSBypassGrant(RLSBypassCallsite):
    owner: str
    expires_on: date


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _exact_scope_source(source: str, node: ast.AST) -> str:
    """Return the exact reviewed source without Python-version-specific AST serialization."""
    segment = ast.get_source_segment(source, node)
    if segment is None:
        raise ValueError("Unable to recover audited bypass scope source")
    return segment


class RLSBypassStaticAnalysisError(ValueError):
    """The reviewed bypass capability escaped the supported static contract."""


@dataclass(slots=True)
class _ParsedModule:
    name: str
    path: Path
    source: str
    tree: ast.Module
    parents: dict[ast.AST, ast.AST]
    bindings: dict[str, str]


@dataclass(slots=True)
class _FunctionSource:
    module: _ParsedModule
    qualname: str
    node: ast.FunctionDef | ast.AsyncFunctionDef

    @property
    def canonical_name(self) -> str:
        return f"{self.module.name}.{self.qualname}"


class _RLSBypassAnalyzer:
    _BYPASS_CANONICAL = "app.database.enter_rls_bypass"

    def __init__(self, app_root: Path):
        self.app_root = app_root
        self.modules: dict[str, _ParsedModule] = {}
        self.functions: dict[str, _FunctionSource] = {}
        self.function_by_node: dict[ast.AST, _FunctionSource] = {}
        self.classes: set[str] = set()
        self.class_sources: dict[str, tuple[_ParsedModule, ast.ClassDef]] = {}
        self.instances: dict[str, str] = {}
        self._digest_entries: set[str] = set()
        self._analysis_visits: set[tuple[str, tuple[str, ...], tuple[str, ...], int]] = set()
        self._load_sources()
        self.wrapper_functions = self._discover_wrapper_functions()
        self.direct_calls = self._validate_bypass_calls()
        self._collect_scopes_and_capability_flows()

    def _error(self, module: _ParsedModule, node: ast.AST, message: str) -> RLSBypassStaticAnalysisError:
        relative = module.path.relative_to(self.app_root.parent).as_posix()
        return RLSBypassStaticAnalysisError(f"{relative}:{getattr(node, 'lineno', '?')}: {message}")

    def _load_sources(self) -> None:
        for path in sorted(self.app_root.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            relative = path.relative_to(self.app_root).with_suffix("")
            module_name = ".".join((self.app_root.name, *relative.parts))
            if module_name.endswith(".__init__"):
                module_name = module_name.removesuffix(".__init__")
            parents: dict[ast.AST, ast.AST] = {}
            for node in ast.walk(tree):
                for child in ast.iter_child_nodes(node):
                    parents[child] = node
            module = _ParsedModule(
                name=module_name,
                path=path,
                source=source,
                tree=tree,
                parents=parents,
                bindings={},
            )
            self.modules[module_name] = module

        for module in self.modules.values():
            self._collect_bindings(module)
            self._collect_definitions(module, module.tree.body, ())
        for module in self.modules.values():
            self._collect_instances(module)

    def _relative_import_module(self, module_name: str, imported: str | None, level: int) -> str:
        if not level:
            return imported or ""
        package = module_name.split(".")[:-1]
        if level > 1:
            package = package[: -(level - 1)]
        suffix = imported.split(".") if imported else []
        return ".".join((*package, *suffix))

    def _bind(self, module: _ParsedModule, local: str, canonical: str, node: ast.AST) -> None:
        previous = module.bindings.get(local)
        if previous is not None and previous != canonical:
            if self._BYPASS_CANONICAL in {previous, canonical}:
                raise self._error(module, node, f"ambiguous RLS bypass import binding {local!r}")
            module.bindings[local] = ""
            return
        module.bindings[local] = canonical

    def _collect_bindings(self, module: _ParsedModule) -> None:
        for node in ast.walk(module.tree):
            if isinstance(node, ast.ImportFrom):
                base = self._relative_import_module(module.name, node.module, node.level)
                for alias in node.names:
                    local = alias.asname or alias.name
                    self._bind(module, local, f"{base}.{alias.name}" if base else alias.name, node)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    canonical = alias.name if alias.asname else alias.name.split(".")[0]
                    self._bind(module, local, canonical, node)

    def _collect_definitions(self, module: _ParsedModule, body: list[ast.stmt], prefix: tuple[str, ...]) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                qualname = ".".join((*prefix, node.name))
                canonical = f"{module.name}.{qualname}"
                self.classes.add(canonical)
                self.class_sources[canonical] = (module, node)
                self._collect_definitions(module, node.body, (*prefix, node.name))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = ".".join((*prefix, node.name))
                function = _FunctionSource(module=module, qualname=qualname, node=node)
                self.functions[function.canonical_name] = function
                self.function_by_node[node] = function
                self._collect_definitions(module, node.body, (*prefix, node.name))

    def _collect_instances(self, module: _ParsedModule) -> None:
        for node in module.tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not isinstance(node.value, ast.Call):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            owner = self._resolve_class(module, node.value.func)
            if owner is None:
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    self.instances[f"{module.name}.{target.id}"] = owner

    def _canonical_expr(self, module: _ParsedModule, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return module.bindings.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = self._canonical_expr(module, node.value)
            return f"{base}.{node.attr}" if base else None
        return None

    def _nearest_class(self, function: _FunctionSource | None) -> str | None:
        if function is None:
            return None
        parts = function.qualname.split(".")[:-1]
        while parts:
            candidate = f"{function.module.name}.{'.'.join(parts)}"
            if candidate in self.classes:
                return candidate
            parts.pop()
        return None

    def _resolve_class(self, module: _ParsedModule, node: ast.expr) -> str | None:
        canonical = self._canonical_expr(module, node)
        if canonical in self.classes:
            return canonical
        local = f"{module.name}.{canonical}" if canonical else None
        return local if local in self.classes else None

    def _resolve_local_instance(
        self,
        module: _ParsedModule,
        function: _FunctionSource | None,
        name: str,
    ) -> str | None:
        if function is None:
            return None
        owners: set[str] = set()
        found = False
        for node in self._runtime_nodes(function.node.body):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not any(isinstance(target, ast.Name) and target.id == name for target in targets):
                continue
            found = True
            if not isinstance(node.value, ast.Call):
                return None
            owner = self._resolve_class(module, node.value.func)
            if owner is None:
                return None
            owners.add(owner)
        return next(iter(owners)) if found and len(owners) == 1 else None

    def _resolve_function(
        self,
        module: _ParsedModule,
        node: ast.expr,
        current: _FunctionSource | None,
    ) -> str | None:
        if isinstance(node, ast.Name):
            canonical = module.bindings.get(node.id)
            if canonical in self.functions:
                return canonical
            if canonical == self._BYPASS_CANONICAL or (canonical is None and node.id == "enter_rls_bypass"):
                return self._BYPASS_CANONICAL
            if current is not None:
                parts = current.qualname.split(".")
                for end in range(len(parts), 0, -1):
                    candidate = f"{module.name}.{'.'.join((*parts[:end], node.id))}"
                    if candidate in self.functions:
                        return candidate
            candidate = f"{module.name}.{node.id}"
            return candidate if candidate in self.functions else None
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in {"self", "cls"}:
                owner = self._nearest_class(current)
                candidate = f"{owner}.{node.attr}" if owner else None
                return candidate if candidate in self.functions else None
            if isinstance(node.value, ast.Call):
                owner = self._resolve_class(module, node.value.func)
                candidate = f"{owner}.{node.attr}" if owner else None
                return candidate if candidate in self.functions else None
            canonical = self._canonical_expr(module, node)
            if canonical == self._BYPASS_CANONICAL:
                return canonical
            value_name = self._canonical_expr(module, node.value) or ""
            instance_owner = self.instances.get(value_name) or self.instances.get(f"{module.name}.{value_name}")
            if instance_owner is None and isinstance(node.value, ast.Name):
                instance_owner = self._resolve_local_instance(module, current, node.value.id)
            if instance_owner is not None:
                candidate = f"{instance_owner}.{node.attr}"
                return candidate if candidate in self.functions else None
            return canonical if canonical in self.functions else None
        return None

    def _enclosing_function(self, module: _ParsedModule, node: ast.AST) -> _FunctionSource | None:
        current = node
        while current in module.parents:
            current = module.parents[current]
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return self.function_by_node.get(current)
        return None

    def _is_dynamic_bypass_lookup(self, module: _ParsedModule, node: ast.AST) -> bool:
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and self._canonical_expr(module, node.args[0]) == "app.database"
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "enter_rls_bypass"
        ):
            return True
        if not isinstance(node, ast.Subscript):
            return False
        key = node.slice
        if not isinstance(key, ast.Constant) or key.value != "enter_rls_bypass":
            return False
        if self._canonical_expr(module, node.value) in {
            "app.database",
            "app.database.__dict__",
        }:
            return True
        return bool(
            isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "vars"
            and node.value.args
            and self._canonical_expr(module, node.value.args[0]) == "app.database"
        )

    def _context_item(
        self, module: _ParsedModule, call: ast.Call
    ) -> tuple[ast.With | ast.AsyncWith, ast.withitem] | None:
        parent = module.parents.get(call)
        if not isinstance(parent, ast.withitem) or parent.context_expr is not call:
            return None
        owner = module.parents.get(parent)
        if not isinstance(owner, (ast.With, ast.AsyncWith)):
            return None
        return owner, parent

    def _is_contextmanager(self, function: _FunctionSource) -> bool:
        return any(
            _decorator_name(decorator) in {"contextmanager", "asynccontextmanager"}
            for decorator in function.node.decorator_list
        ) and any(isinstance(node, (ast.Yield, ast.YieldFrom)) for node in ast.walk(function.node))

    def _discover_wrapper_functions(self) -> set[str]:
        candidates = {name for name, function in self.functions.items() if self._is_contextmanager(function)}
        wrappers: set[str] = set()
        changed = True
        while changed:
            changed = False
            for name in candidates - wrappers:
                function = self.functions[name]
                for node in ast.walk(function.node):
                    if not isinstance(node, ast.Call) or self._context_item(function.module, node) is None:
                        continue
                    target = self._resolve_function(function.module, node.func, function)
                    if target == self._BYPASS_CANONICAL or target in wrappers:
                        wrappers.add(name)
                        changed = True
                        break
        return wrappers

    def _validate_bypass_calls(self) -> list[tuple[_ParsedModule, ast.Call]]:
        direct_calls: list[tuple[_ParsedModule, ast.Call]] = []
        for module in self.modules.values():
            for node in ast.walk(module.tree):
                if self._is_dynamic_bypass_lookup(module, node):
                    raise self._error(module, node, "dynamic RLS bypass lookup is unsupported")
                if not isinstance(node, ast.Call):
                    continue
                current = self._enclosing_function(module, node)
                target = self._resolve_function(module, node.func, current)
                if target != self._BYPASS_CANONICAL and target not in self.wrapper_functions:
                    continue
                if self._context_item(module, node) is None:
                    raise self._error(module, node, "RLS bypass callable must be the direct context expression")
                if target == self._BYPASS_CANONICAL:
                    direct_calls.append((module, node))

        protected = {self._BYPASS_CANONICAL, *self.wrapper_functions}
        for module in self.modules.values():
            for node in ast.walk(module.tree):
                if not isinstance(node, (ast.Name, ast.Attribute)):
                    continue
                parent = module.parents.get(node)
                if isinstance(parent, ast.Attribute) and parent.value is node:
                    continue
                current = self._enclosing_function(module, node)
                target = self._resolve_function(module, node, current)
                if target not in protected:
                    continue
                if isinstance(parent, ast.Call) and parent.func is node:
                    continue
                if target in self.wrapper_functions and isinstance(parent, ast.keyword):
                    call = module.parents.get(parent)
                    if (
                        isinstance(call, ast.Call)
                        and self._resolve_function(module, call.func, current) in self.functions
                    ):
                        continue
                raise self._error(module, node, "RLS bypass callable alias or dynamic escape is unsupported")
        return direct_calls

    def _add_function_digest(self, function: _FunctionSource) -> None:
        self._digest_entries.add(
            "\0".join(
                (
                    function.module.path.relative_to(self.app_root.parent).as_posix(),
                    function.qualname,
                    _exact_scope_source(function.module.source, function.node),
                )
            )
        )
        # Module globals and import bindings can change the meaning of a reviewed
        # helper without changing its function body, so review the containing module too.
        self._digest_entries.add(
            "\0".join(
                (
                    function.module.path.relative_to(self.app_root.parent).as_posix(),
                    "<module-source>",
                    function.module.source,
                )
            )
        )

    def _add_class_digest(self, canonical_name: str) -> None:
        module, node = self.class_sources[canonical_name]
        self._digest_entries.add(
            "\0".join(
                (
                    module.path.relative_to(self.app_root.parent).as_posix(),
                    canonical_name.removeprefix(f"{module.name}."),
                    _exact_scope_source(module.source, node),
                )
            )
        )
        self._digest_entries.add(
            "\0".join(
                (
                    module.path.relative_to(self.app_root.parent).as_posix(),
                    "<module-source>",
                    module.source,
                )
            )
        )

    def _parameter_taints(
        self,
        module: _ParsedModule,
        call: ast.Call,
        function: _FunctionSource,
        tainted_names: set[str],
    ) -> set[str]:
        positional = [*function.node.args.posonlyargs, *function.node.args.args]
        if isinstance(call.func, ast.Attribute) and positional and positional[0].arg in {"self", "cls"}:
            positional = positional[1:]
        tainted: set[str] = set()
        for index, argument in enumerate(call.args):
            hits = {name for name in tainted_names if self._capability_value_escape(argument, {name})}
            if not hits:
                continue
            if not isinstance(argument, ast.Name) or argument.id not in tainted_names or index >= len(positional):
                raise self._error(module, argument, "RLS bypass capability cannot be wrapped in a dynamic argument")
            tainted.add(positional[index].arg)
        keyword_parameters = {argument.arg for argument in (*positional, *function.node.args.kwonlyargs)}
        for keyword in call.keywords:
            hits = {name for name in tainted_names if self._capability_value_escape(keyword.value, {name})}
            if not hits:
                continue
            if (
                keyword.arg is None
                or keyword.arg not in keyword_parameters
                or not isinstance(keyword.value, ast.Name)
                or keyword.value.id not in tainted_names
            ):
                raise self._error(module, keyword.value, "RLS bypass capability cannot use **kwargs or containers")
            tainted.add(keyword.arg)
        return tainted

    def _runtime_nodes(self, body: list[ast.stmt]):
        stack: list[ast.AST] = list(reversed(body))
        while stack:
            node = stack.pop()
            yield node
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            stack.extend(reversed(list(ast.iter_child_nodes(node))))

    def _contains_capability_reference(self, node: ast.AST, names: set[str]) -> bool:
        return any(isinstance(item, ast.Name) and item.id in names for item in ast.walk(node))

    def _capability_value_escape(self, node: ast.AST, names: set[str]) -> bool:
        if isinstance(node, ast.Name):
            return node.id in names
        if isinstance(node, ast.Attribute):
            return self._capability_value_escape(node.value, names)
        if isinstance(node, ast.Subscript):
            return self._capability_value_escape(node.value, names)
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return any(self._capability_value_escape(item, names) for item in node.elts)
        if isinstance(node, ast.Dict):
            return any(
                self._capability_value_escape(item, names) for item in (*node.keys, *node.values) if item is not None
            )
        if isinstance(node, (ast.Starred, ast.Lambda)):
            return self._contains_capability_reference(node, names)
        if isinstance(node, ast.IfExp):
            return self._capability_value_escape(node.body, names) or self._capability_value_escape(node.orelse, names)
        if isinstance(node, ast.BoolOp):
            return any(self._capability_value_escape(item, names) for item in node.values)
        return False

    def _analyze_function_capabilities(
        self,
        function: _FunctionSource,
        *,
        session_names: set[str],
        factory_names: set[str],
    ) -> None:
        body_line = function.node.body[0].lineno if function.node.body else function.node.lineno
        visit = (function.canonical_name, tuple(sorted(session_names)), tuple(sorted(factory_names)), body_line)
        if visit in self._analysis_visits:
            return
        self._analysis_visits.add(visit)
        self._add_function_digest(function)
        module = function.module

        for node in self._runtime_nodes(function.node.body):
            if isinstance(node, (ast.Return, ast.Yield, ast.YieldFrom)) and node.value is not None:
                if self._capability_value_escape(node.value, session_names | factory_names):
                    if (
                        isinstance(node, ast.Yield)
                        and function.canonical_name in self.wrapper_functions
                        and isinstance(node.value, ast.Name)
                        and node.value.id in session_names
                    ):
                        continue
                    raise self._error(module, node, "RLS bypass capability cannot be returned or yielded")
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                value = getattr(node, "value", None)
                if value is not None and self._capability_value_escape(value, session_names | factory_names):
                    raise self._error(module, node, "RLS bypass capability aliases are unsupported")
            if not isinstance(node, ast.Call):
                continue

            if isinstance(node.func, ast.Name) and node.func.id in factory_names:
                context = self._context_item(module, node)
                if context is None:
                    raise self._error(module, node, "RLS bypass session factory must be used directly by with")
                scope, item = context
                if not isinstance(item.optional_vars, ast.Name):
                    raise self._error(module, item, "RLS bypass session factory must bind one simple name")
                self._analyze_statements(
                    module,
                    self._enclosing_function(module, scope),
                    scope.body,
                    {item.optional_vars.id},
                )
                continue

            tainted_arguments = {
                name
                for argument in (*node.args, *(keyword.value for keyword in node.keywords))
                for name in session_names | factory_names
                if self._capability_value_escape(argument, {name})
            }
            if not tainted_arguments:
                continue
            if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                if node.func.value.id in session_names and not any(
                    name in session_names | factory_names for name in tainted_arguments if name != node.func.value.id
                ):
                    continue
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in session_names
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                # Exact attribute introspection stays inside the already-digested
                # helper module; dynamic names and capability export still fail closed.
                continue
            if (
                isinstance(node.func, ast.Name)
                and node.func.id in {"hasattr", "isinstance"}
                and (
                    node.func.id != "hasattr"
                    or (
                        len(node.args) >= 2
                        and isinstance(node.args[1], ast.Constant)
                        and isinstance(node.args[1].value, str)
                    )
                )
            ):
                continue
            owner = self._resolve_class(module, node.func)
            if owner is not None:
                parent = module.parents.get(node)
                targets = (
                    parent.targets
                    if isinstance(parent, ast.Assign)
                    else [parent.target]
                    if isinstance(parent, ast.AnnAssign)
                    else []
                )
                if len(targets) != 1 or not isinstance(targets[0], ast.Name):
                    raise self._error(module, node, "RLS bypass session-backed object must bind one simple name")
                self._add_class_digest(owner)
                session_names.add(targets[0].id)
                continue
            target = self._resolve_function(module, node.func, function)
            if target == self._BYPASS_CANONICAL:
                # Direct bypass scopes are validated and analyzed independently.
                # Re-entering one from a tainted helper must not traverse the
                # contextmanager implementation as if it were an ordinary callback.
                continue
            if target is None or target not in self.functions:
                raise self._error(module, node, "RLS bypass session reached an unresolved callback")
            target_function = self.functions[target]
            session_parameters = self._parameter_taints(module, node, target_function, session_names)
            factory_parameters = self._parameter_taints(module, node, target_function, factory_names)
            self._analyze_function_capabilities(
                target_function,
                session_names=session_parameters,
                factory_names=factory_parameters,
            )

    def _analyze_statements(
        self,
        module: _ParsedModule,
        function: _FunctionSource | None,
        body: list[ast.stmt],
        session_names: set[str],
    ) -> None:
        if function is None:
            raise self._error(module, body[0] if body else module.tree, "module-level bypass scope is unsupported")
        synthetic = _FunctionSource(module=module, qualname=function.qualname, node=function.node)
        original_body = synthetic.node.body
        synthetic.node.body = body
        try:
            self._analyze_function_capabilities(synthetic, session_names=session_names, factory_names=set())
        finally:
            synthetic.node.body = original_body

    def _collect_scopes_and_capability_flows(self) -> None:
        protected = {self._BYPASS_CANONICAL, *self.wrapper_functions}
        for module in self.modules.values():
            for scope in (node for node in ast.walk(module.tree) if isinstance(node, (ast.With, ast.AsyncWith))):
                for item in scope.items:
                    if not isinstance(item.context_expr, ast.Call):
                        continue
                    function = self._enclosing_function(module, scope)
                    target = self._resolve_function(module, item.context_expr.func, function)
                    if target not in protected:
                        continue
                    self._digest_entries.add(
                        "\0".join(
                            (
                                module.path.relative_to(self.app_root.parent).as_posix(),
                                function.qualname if function else "<module>",
                                _exact_scope_source(module.source, scope),
                            )
                        )
                    )
                    if item.optional_vars is not None and not isinstance(item.optional_vars, ast.Name):
                        raise self._error(module, item, "RLS bypass scope must bind one simple name")
                    session_names: set[str] = set()
                    if target == self._BYPASS_CANONICAL:
                        if item.context_expr.args:
                            session_argument = item.context_expr.args[0]
                        else:
                            session_argument = next(
                                (keyword.value for keyword in item.context_expr.keywords if keyword.arg == "session"),
                                None,
                            )
                        if isinstance(session_argument, ast.Name):
                            session_names.add(session_argument.id)
                        elif session_argument is not None:
                            raise self._error(
                                module,
                                session_argument,
                                "RLS bypass session argument must be one simple name",
                            )
                    if isinstance(item.optional_vars, ast.Name):
                        session_names.add(item.optional_vars.id)
                    if session_names:
                        self._analyze_statements(module, function, scope.body, session_names)

        for module in self.modules.values():
            for node in ast.walk(module.tree):
                if not isinstance(node, ast.Call):
                    continue
                current = self._enclosing_function(module, node)
                target = self._resolve_function(module, node.func, current)
                if target is None or target not in self.functions:
                    continue
                wrapper_arguments: set[str] = set()
                for argument in (*node.args, *(keyword.value for keyword in node.keywords)):
                    resolved = (
                        self._resolve_function(module, argument, current) if isinstance(argument, ast.expr) else None
                    )
                    if resolved in self.wrapper_functions and isinstance(argument, (ast.Name, ast.Attribute)):
                        wrapper_arguments.add(argument.id if isinstance(argument, ast.Name) else argument.attr)
                if not wrapper_arguments:
                    continue
                if current is not None:
                    self._add_function_digest(current)
                factory_parameters = self._parameter_taints(module, node, self.functions[target], wrapper_arguments)
                self._analyze_function_capabilities(
                    self.functions[target],
                    session_names=set(),
                    factory_names=factory_parameters,
                )

    def fingerprint(self) -> str:
        return hashlib.sha256("\n".join(sorted(self._digest_entries)).encode("utf-8")).hexdigest()


def fingerprint_rls_bypass_scopes(app_root: Path) -> str:
    """Fingerprint exact bypass scopes and every statically tracked capability consumer."""
    return _RLSBypassAnalyzer(app_root).fingerprint()


# Reviewed normalized AST of direct bypass scopes and statically discoverable
# contextmanager consumers. Predicates, locks, ORM writes, and add() targets are included.
RLS_BYPASS_SCOPES_SHA256 = "d1001916ff05c802132ae54e70f85fd00a397c98cc32e2c7bc313d7d7d4bd20d"


def scan_rls_bypass_callsites(app_root: Path) -> list[RLSBypassCallsite]:
    analyzer = _RLSBypassAnalyzer(app_root)
    callsites: list[RLSBypassCallsite] = []
    for module, node in analyzer.direct_calls:
        context = analyzer._context_item(module, node)
        if context is None:  # already enforced by the analyzer
            raise analyzer._error(module, node, "RLS bypass callsite has no scope")
        bypass_scope, _ = context
        enclosing = analyzer._enclosing_function(module, node)

        reason = next(
            (ast.unparse(keyword.value) for keyword in node.keywords if keyword.arg == "reason"),
            "",
        )
        query_fields: list[str] = []
        for nested in ast.walk(bypass_scope):
            if not isinstance(nested, ast.Call):
                continue
            operation = _call_name(nested)
            if operation not in {"select", "insert", "update", "delete"}:
                continue
            arguments = ",".join(ast.unparse(argument) for argument in nested.args)
            query_fields.append(f"{operation}:{arguments}")
        if not query_fields:
            query_fields.append("session-state-only")

        callsites.append(
            RLSBypassCallsite(
                file=module.path.relative_to(app_root.parent).as_posix(),
                function=enclosing.node.name if enclosing else "<module>",
                reason_expression=reason,
                allowed_query_fields=tuple(dict.fromkeys(query_fields)),
            )
        )
    return callsites


def _grant(
    file: str,
    function: str,
    reason_expression: str,
    allowed_query_fields: tuple[str, ...],
) -> RLSBypassGrant:
    return RLSBypassGrant(
        file=file,
        function=function,
        reason_expression=reason_expression,
        allowed_query_fields=allowed_query_fields,
        owner="platform-security",
        expires_on=date(2026, 10, 31),
    )


RLS_BYPASS_ALLOWLIST = (
    _grant(*("app/api/admin.py", "_load_platform_admin_agent_and_pin", "reason", ("select:Agent",))),
    _grant(
        *(
            "app/api/admin.py",
            "list_companies",
            "'platform admin company list and cross-tenant stats'",
            (
                "select:sqla_func.coalesce(sqla_func.sum(Agent.tokens_used_total), 0)",
                "select:Tenant",
                "select:sqla_func.count()",
                "select:User.email",
            ),
        )
    ),
    _grant(
        *(
            "app/api/admin.py",
            "toggle_company",
            "'platform admin company status transition'",
            ("select:Tenant",),
        )
    ),
    _grant(
        *(
            "app/scripts/audit_tenant_null_semantics.py",
            "audit_tenant_null_semantics",
            "'tenant NULL semantics read-only audit'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/scripts/repair_knowledge_provenance.py",
            "_main",
            "'append-only cross-tenant Knowledge provenance repair'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/scripts/storage_lifecycle.py",
            "_authoritative_agent_tenants",
            "'storage lifecycle fleet Agent tenant authority inventory'",
            ("select:Agent.id,Agent.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/api/admin.py",
            "get_metrics_timeseries",
            "'platform admin metrics timeseries cross-tenant aggregation'",
            (
                "select:sqla_func.coalesce(sqla_func.sum(TokenUsageEvent.tokens), 0)",
                "select:sqla_func.date(Tenant.created_at).label('d'),sqla_func.count().label('cnt')",
                "select:sqla_func.date(User.created_at).label('d'),sqla_func.count().label('cnt')",
                "select:sqla_func.date(TokenUsageEvent.created_at).label('d'),sqla_func.coalesce(sqla_func.sum(TokenUsageEvent.tokens), 0).label('tokens')",
                "select:sqla_func.count()",
            ),
        )
    ),
    _grant(
        *(
            "app/api/admin.py",
            "get_metrics_leaderboards",
            "'platform admin metrics leaderboard cross-tenant aggregation'",
            (
                "select:Agent.name,Tenant.name.label('company'),agent_tokens.label('tokens')",
                "select:Tenant.name,sqla_func.coalesce(sqla_func.sum(Agent.tokens_used_total), 0).label('tokens')",
            ),
        )
    ),
    _grant(
        *(
            "app/api/auth.py",
            "register",
            "'public registration uniqueness and bootstrap checks'",
            ("select:User", "select:func.count()", "select:Tenant"),
        )
    ),
    _grant(
        *(
            "app/services/feishu_app_registration.py",
            "_persist_registered_credentials",
            "f'Feishu registration actor revalidation for {context.session_id}'",
            ("select:User",),
        )
    ),
    _grant(
        *(
            "app/services/startup_bootstrap.py",
            "ensure_default_tenant",
            "'startup default tenant bootstrap'",
            ("insert:Tenant",),
        )
    ),
    _grant(*("app/api/auth.py", "login", "'public login identifier lookup'", ("select:User", "select:Tenant"))),
    _grant(
        *(
            "app/api/channel_rls.py",
            "load_public_agent_channel_config",
            "f'public {channel_type} webhook channel lookup for agent {agent_id}'",
            ("select:ChannelConfig", "select:Agent.tenant_id", "select:Tenant.is_active"),
        )
    ),
    _grant(
        *(
            "app/api/feishu.py",
            "_load_public_sso_scan_session",
            "f'public feishu sso session lookup for {session_id}'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/api/feishu.py",
            "feishu_card_callback",
            "f'public feishu card approval lookup for {approval_uuid}'",
            ("select:ApprovalRequest", "select:AgentModel"),
        )
    ),
    _grant(*("app/api/tenants.py", "self_create_company", "'self-service company creation'", ("select:User",))),
    _grant(
        *(
            "app/api/tenants.py",
            "join_company",
            "'tenant join invitation lookup'",
            ("select:InvitationCode", "select:Tenant", "select:User"),
        )
    ),
    _grant(*("app/api/tenants.py", "list_tenants", "'platform-admin list tenants'", ("select:Tenant",))),
    _grant(
        *(
            "app/api/tenants.py",
            "_assign_user_to_tenant",
            "'platform-admin assign user to tenant'",
            ("select:Tenant", "select:User"),
        )
    ),
    _grant(*("app/api/tenants.py", "_platform_admin_bypass_scope", "reason", ("session-state-only",))),
    _grant(
        *(
            "app/api/webhooks.py",
            "receive_webhook",
            "'public webhook token resolution (tenant unknown until trigger found)'",
            ("select:AgentTrigger",),
        )
    ),
    _grant(
        *(
            "app/core/permissions.py",
            "_load_agent_for_user",
            "f'platform-admin agent access lookup for {agent_id}'",
            ("select:Agent",),
        )
    ),
    _grant(
        *(
            "app/core/permissions.py",
            "require_agent_owner_or_admin",
            "f'platform-admin agent ownership lookup for {agent_id}'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/core/security.py",
            "verify_refresh_token",
            "'refresh token lookup'",
            ("select:RefreshToken", "select:User.tenant_id"),
        )
    ),
    _grant(
        *(
            "app/core/security.py",
            "revoke_refresh_token",
            "'refresh token revoke lookup'",
            ("select:RefreshToken", "select:User.tenant_id"),
        )
    ),
    _grant(
        *(
            "app/core/security.py",
            "authenticate_request_user",
            "'platform-admin identity lookup before selected-tenant override'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/core/security.py",
            "authenticate_request_user",
            "'tenantless authenticated identity lookup before company bootstrap'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/scripts/backfill_mcp_tool_names.py",
            "_run",
            "'Step 6 MCP canonical name backfill'",
            ("select:Tool", "update:Tool"),
        )
    ),
    _grant(
        *(
            "app/scripts/cleanup_duplicate_feishu_users.py",
            "main",
            "'feishu identity maintenance: backfill+merge users across all tenants'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/scripts/migrate_schedules_to_triggers.py",
            "migrate",
            "'one-time schedule→trigger migration across all agents'",
            (
                "select:AgentSchedule.id,AgentSchedule.agent_id,AgentSchedule.name,AgentSchedule.instruction,AgentSchedule.cron_expr,AgentSchedule.is_enabled,AgentSchedule.run_count,AgentSchedule.last_run_at",
                "select:AgentTrigger",
            ),
        )
    ),
    _grant(
        *(
            "app/scripts/repair_false_tool_evidence_notices.py",
            "_main",
            "'audited cross-tenant repair of exact retired tool-evidence verifier notices'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/agent_identity_lifecycle.py",
            "ensure_agent_identity",
            "rls_bypass_reason",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/agent_seeder.py",
            "seed_default_agents",
            "'first-startup default-agent seeding: resolve platform admin + seed Morty/Meeseeks'",
            ("select:SystemSetting", "select:Agent", "select:Skill", "select:Tool", "select:User"),
        )
    ),
    _grant(
        *(
            "app/services/approval_ticket.py",
            "consume_approval_ticket",
            "'approval ticket tenant locator'",
            ("select:ApprovalRequest.tenant_id,ApprovalRequest.agent_id",),
        )
    ),
    _grant(
        *(
            "app/services/approval_ticket.py",
            "reconcile_stuck_approval_tickets",
            "'approval ticket execution reconciliation locator'",
            ("select:ApprovalRequest.id,ApprovalRequest.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/services/business_task_runtime.py",
            "finalize_business_task_execution",
            "'business task finalization locator'",
            ("select:RuntimeTask.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/services/channel_ingress_inbox.py",
            "_worker_session",
            "f'channel_ingress_inbox.{operation}'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/channel_delivery_outbox.py",
            "_worker_session",
            "f'channel_delivery_outbox.{operation}'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/budget_transition_outbox.py",
            "_worker_session",
            "f'budget_transition_outbox.{operation}'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/business_task_runtime.py",
            "mark_business_task_execution_started",
            "'business task runtime locator'",
            ("select:RuntimeTask.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/services/delegation_session_repair.py",
            "_projection_truth",
            "'peer delegation Session repair verification'",
            ("select:RuntimeTask", "select:ChatSession.id", "select:ChatTranscriptEvent.run_id"),
        )
    ),
    _grant(
        *(
            "app/services/delegation_session_repair.py",
            "repair_peer_delegation_session_projections",
            "'peer delegation Session repair scan'",
            ("select:RuntimeTask",),
        )
    ),
    _grant(
        *(
            "app/services/code_execution/probe.py",
            "store_latest_sandbox_probe_evidence",
            "'code execution sandbox probe latest evidence write'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/code_execution/probe.py",
            "latest_sandbox_probe_health",
            "'code execution sandbox probe latest evidence health read'",
            ("select:SystemSetting",),
        )
    ),
    _grant(
        *(
            "app/services/decision_trace.py",
            "_tenant_for_decision",
            "f'decision feedback tenant resolution for {normalized_id}'",
            ("select:DecisionTraceRecord.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/services/dingtalk_stream.py",
            "start_all",
            "'dingtalk start_all — enumerate all configured channels across tenants'",
            ("select:ChannelConfig",),
        )
    ),
    _grant(
        *(
            "app/services/evolution_daemon.py",
            "_bypass_session",
            "'personal-kb import job drain — recover stale queued/running person-scope jobs'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/evolution_daemon.py",
            "_drain_company_kb_jobs",
            "'company-kb import job drain — recover queued and stale claimed jobs'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/evolution_daemon.py",
            "_heartbeat_loop",
            "'pending-reply expiry sweep — expire stale contexts across all tenants'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/feishu_ws.py",
            "start_all",
            "'feishu start_all — enumerate all configured channels across tenants'",
            ("select:ChannelConfig",),
        )
    ),
    _grant(
        *(
            "app/services/heartbeat.py",
            "_run_chat_artifact_snapshot_retention",
            "'chat artifact snapshot retention across tenants'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/heartbeat.py",
            "_heartbeat_tick",
            "'heartbeat tick — enumerate all running/idle agents across tenants'",
            ("select:Agent",),
        )
    ),
    _grant(
        *(
            "app/services/heartbeat.py",
            "_workspace_full_sweep",
            "'workspace full sweep — enumerate active tenants across all agents'",
            ("select:Agent.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/services/hook_runtime_config.py",
            "apply_all_persisted_hook_runtime_configs",
            "'hook runtime config startup load'",
            ("select:SystemSetting",),
        )
    ),
    _grant(
        *(
            "app/services/local_agent_channel_service.py",
            "resolve_ws_ticket",
            "'local agent channel ws ticket lookup'",
            ("select:LocalAgentChannelWsTicket,LocalAgentBridgeConnection,User,Tenant",),
        )
    ),
    _grant(
        *(
            "app/services/local_agent_channel_service.py",
            "resolve_browser_session_ws_ticket",
            "'local agent browser ws ticket actor lookup'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/local_bridge_service.py",
            "_pairing_identity_is_live",
            "'local bridge pairing live identity check'",
            ("select:User.id",),
        )
    ),
    _grant(
        *(
            "app/services/local_bridge_service.py",
            "_load_pairing_by_user_code",
            "'local bridge user-code pairing lookup'",
            ("select:LocalAgentBridgePairingSession",),
        )
    ),
    _grant(
        *(
            "app/services/local_bridge_service.py",
            "create_pairing_session",
            "'anonymous local bridge pairing init (unbound pending holding scope)'",
            ("insert:LocalAgentBridgePairingSession", "insert:Tenant", "select:LocalAgentBridgePairingSession"),
        )
    ),
    _grant(
        *(
            "app/services/local_bridge_service.py",
            "approve_pairing_session",
            "'local bridge pairing approval tenant rebind'",
            ("update:LocalAgentBridgePairingSession",),
        )
    ),
    _grant(
        *(
            "app/services/local_bridge_service.py",
            "reject_pairing_session",
            "'local bridge pairing reject tenant rebind'",
            ("update:LocalAgentBridgePairingSession",),
        )
    ),
    _grant(
        *(
            "app/services/local_bridge_service.py",
            "_load_pairing_by_device_code",
            "'local bridge device-code pairing lookup'",
            ("select:LocalAgentBridgePairingSession",),
        )
    ),
    _grant(
        *(
            "app/services/local_bridge_service.py",
            "resolve_bridge_auth_context",
            "'local bridge bearer token lookup'",
            ("select:LocalAgentBridgeConnection",),
        )
    ),
    _grant(
        *(
            "app/services/plan_mode_cutover.py",
            "mark_existing_triggers_plan_exempt",
            "'plan-mode cutover grandfather (cross-tenant trigger exemption)'",
            ("select:AgentTrigger",),
        )
    ),
    _grant(
        *(
            "app/services/plugin_hook_service.py",
            "register_installed_plugin_hooks",
            "'plugin hook startup registration'",
            ("select:PluginHookRegistration.tenant_id",),
        )
    ),
    _grant(
        *("app/services/resource_discovery.py", "_load", "'global ModelScope API token config read'", ("select:Tool",))
    ),
    _grant(*("app/services/resource_discovery.py", "_load", "'global Smithery API key config read'", ("select:Tool",))),
    _grant(
        *(
            "app/services/runtime_budget_service.py",
            "_budget_session",
            "f'runtime_budget_service.{operation}'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/runtime_notification_outbox.py",
            "_worker_session",
            "f'runtime_notification_outbox.{operation}'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/team_fanout_recovery.py",
            "_worker_session",
            "f'team_fanout_recovery.{operation}'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/runtime_control_bus.py",
            "sweep_pending_transcript_t0_bridges",
            "'sweep pending transcript T0 projections'",
            ("select:ChatTranscriptEvent.id",),
        )
    ),
    _grant(
        *(
            "app/services/runtime_task_service.py",
            "list_active_runtime_task_records",
            "'restart-safe async-delegation resume scan'",
            ("select:RuntimeTask.id,RuntimeTask.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/services/runtime_task_service.py",
            "reconcile_orphaned_runtime_tasks",
            "'startup orphaned runtime-task reconcile'",
            ("select:RuntimeTask.id,RuntimeTask.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/scripts/reconcile_orphaned_trigger_runs.py",
            "_collect_orphans",
            "'reconcile orphaned trigger runtime tasks'",
            ("select:RuntimeTask.id,RuntimeTask.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/services/hr_creation_reconciliation.py",
            "reconcile_hr_creation_drafts_once",
            "'HR draft expiry and orphaned provisioning reconciliation'",
            ("select:HrCreationDraft.id,HrCreationDraft.provisioning_task_id",),
        )
    ),
    _grant(
        *(
            "app/services/business_task_reconciliation.py",
            "reconcile_stale_business_tasks_once",
            "'BusinessTask expired worker lease reconciliation'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/runtime_task_worker.py",
            "claim_and_dispatch_once",
            "'runtime task worker claim pending executable tasks'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/runtime_task_worker.py",
            "_discover_terminal_boundary_tenants",
            "'runtime task worker discover terminal boundary tenants'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/runtime_task_worker.py",
            "recover_session_control_inputs_once",
            "'runtime task worker recover stale Session V2 control inputs'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/runtime_task_worker.py",
            "expire_session_permission_requests_once",
            "'runtime task worker expire Session V2 permission requests'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/runtime_task_worker.py",
            "recover_session_model_rounds_once",
            "'runtime task worker recover sealed Session V2 model rounds'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/runtime_task_worker.py",
            "recover_session_terminal_outcomes_once",
            "'runtime task worker recover sealed Session V2 terminal outcomes'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/runtime_task_worker.py",
            "recover_session_terminal_outcomes_once",
            "'runtime task worker recover Session V2 terminal candidates'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/runtime_task_worker.py",
            "recover_turn_replacement_sagas_once",
            "'runtime task worker recover Session V2 turn replacement sagas'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/runtime_task_worker.py",
            "recover_stale_session_input_admissions_once",
            "'runtime task worker recover stale Session V2 input admissions'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/runtime_task_worker.py",
            "recover_session_input_dispatches_once",
            "'runtime task worker dispatch admitted Session V2 inputs'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/runtime_task_worker.py",
            "recover_terminal_target_session_inputs_once",
            "'runtime task worker roll over Session V2 steers after target run terminal'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/session_event_outbox.py",
            "_worker_session",
            "f'session_event_outbox.{operation}'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/session_v2_persistence.py",
            "resolve_session_command_authority",
            "'session command platform-admin actor recovery lookup'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/session_workspace_snapshot.py",
            "recover_workspace_restores_from_transcript",
            "'workspace restore crash recovery'",
            ("select:ChatTranscriptEvent.id", "select:AuditLog.id"),
        )
    ),
    _grant(
        *(
            "app/services/audit_logger.py",
            "write_audit_log",
            "'operator system audit log insert'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/audit_logger.py",
            "write_platform_security_audit_event",
            "'operator platform security audit insert'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/platform_security_audit.py",
            "query_platform_security_audit_events",
            "'operator platform security audit query'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/platform_security_audit.py",
            "verify_persisted_platform_security_audit_chain",
            "'operator platform security audit chain verification'",
            ("session-state-only",),
        )
    ),
    _grant(
        *(
            "app/services/skill_seeder.py",
            "seed_skills",
            "'startup builtin skill registry seed'",
            ("select:Skill",),
        )
    ),
    _grant(
        *(
            "app/services/skill_seeder.py",
            "cleanup_retired_builtin_skills",
            "'startup maintenance: remove retired builtin skills across all agent workspaces'",
            ("delete:skill", "select:Agent", "select:Skill"),
        )
    ),
    _grant(
        *(
            "app/services/skill_seeder.py",
            "push_default_skills_to_existing_agents",
            "'startup: push default skills to every existing agent across tenants'",
            ("select:Agent", "select:Skill"),
        )
    ),
    _grant(
        *(
            "app/services/t0_logger.py",
            "backfill_missing_chat_transcript_t0",
            "'startup legacy chat transcript/T0 backfill enumerate'",
            ("select:func.count(ChatTranscriptEvent.id)",),
        )
    ),
    _grant(
        *(
            "app/services/tenant_resolver.py",
            "resolve_tenant_for_agent",
            "f'tenant resolution for agent {agent_uuid}'",
            ("select:Agent.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/services/tenant_resolver.py",
            "resolve_tenant_for_plan",
            "f'tenant resolution for plan {plan_id}'",
            ("select:AgentPlanRequest.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/services/tenant_resolver.py",
            "resolve_tenant_for_runtime_task",
            "f'tenant resolution for runtime task {task_uuid}'",
            ("select:RuntimeTask.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/services/tenant_resolver.py",
            "resolve_tenant_for_user",
            "f'tenant resolution for user {user_uuid}'",
            ("select:User.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/services/tenant_resolver.py",
            "resolve_tenant_for_chat_session",
            "f'tenant resolution for chat session {session_uuid}'",
            ("select:ChatSession.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/services/tenant_resolver.py",
            "resolve_tenant_for_transcript_event",
            "f'tenant resolution for transcript event {event_uuid}'",
            ("select:ChatTranscriptEvent.tenant_id",),
        )
    ),
    _grant(
        *(
            "app/services/tool_seeder.py",
            "seed_builtin_tools",
            "'startup builtin-tool seeding: upsert platform tools + auto-assign to all agents'",
            ("select:Agent.id", "delete:obsolete", "select:Tool"),
        )
    ),
    _grant(
        *(
            "app/services/trigger_daemon.py",
            "_tick",
            "'trigger daemon tick — enumerate all enabled triggers across tenants'",
            ("select:AgentTrigger",),
        )
    ),
    _grant(
        *(
            "app/services/trigger_daemon.py",
            "backfill_null_reply_contexts",
            '"trigger reply_context backfill — enumerate all tenants\' enabled triggers"',
            ("select:AgentTrigger", "select:ChatSession"),
        )
    ),
    _grant(
        *(
            "app/services/wechat_personal_stream.py",
            "start_all",
            "'wechat_personal start_all — enumerate all connected channels across tenants'",
            ("select:ChannelConfig", "select:ExternalPrincipal.id"),
        )
    ),
    _grant(
        *(
            "app/services/wecom_stream.py",
            "start_all",
            "'wecom start_all — enumerate all configured channels across tenants'",
            ("select:ChannelConfig",),
        )
    ),
)
