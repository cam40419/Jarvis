from typing import Any, Dict, List, Tuple, Callable, Optional
import importlib, pkgutil, types

Executor = Callable[[Dict[str, Any]], Tuple[Dict[str, Any], bool]]
ToolSchema = Dict[str, Any]


class ToolRegistry:
    def __init__(self, memory: Optional[Any] = None, cfg: Optional[Any] = None):
        self._schemas: List[Dict[str, Any]] = []
        self._exec: Dict[str, Executor] = {}
        self._memory = memory
        self._cfg = cfg

    def register(self, schema: ToolSchema, executor: Executor) -> None:
        name = schema.get("name")
        if not name:
            raise ValueError("Tool schema must include 'name'")
        if name in self._exec:
            raise ValueError(f"Tool '{name}' already registered")
        self._schemas.append(
            {
                "type": "function",
                "name": schema["name"],
                "description": schema.get("description", ""),
                "parameters": schema.get("parameters", {"type": "object"}),
            }
        )
        self._exec[name] = executor

    def as_openai_tools(self) -> List[Dict[str, Any]]:
        return self._schemas

    def execute(self, name: str, args: Dict[str, Any]):
        fn = self._exec.get(name)
        if not fn:
            return {"error": f"Unknown tool: {name}"}, False
        try:
            return fn(args)
        except Exception as e:
            return {"error": f"Tool '{name}' execution failed: {e}"}, False

    # --- Loading ---

    def load_package(self, package: str) -> None:
        """Load all tools from a package, e.g. 'agent.tools'."""
        pkg = importlib.import_module(package)
        for modinfo in pkgutil.iter_modules(pkg.__path__, package + "."):
            self._load_module(modinfo.name)

    def load_modules(self, module_paths: List[str]) -> None:
        for mp in module_paths:
            self._load_module(mp)

    def _load_module(self, module_path: str) -> None:
        mod = importlib.import_module(module_path)
        get_tool = getattr(mod, "get_tool", None)
        if isinstance(get_tool, types.FunctionType):
            schema, executor = get_tool(memory=self._memory, cfg=self._cfg)
            self.register(schema, executor)
            return
        schema = getattr(mod, "SCHEMA", None)
        executor = getattr(mod, "execute", None)
        if schema and callable(executor):
            self.register(schema, executor)
            return
        raise ValueError(
            f"Module '{module_path}' must define get_tool(memory=None,cfg=None) "
            "or SCHEMA (dict) and execute(args) -> (dict,bool)"
        )
