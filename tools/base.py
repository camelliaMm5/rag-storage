"""Tool base classes: ToolParameter + Tool — thin wrappers for Agent dispatch."""
from pydantic import BaseModel, create_model


_PARAM_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}


class ToolParameter:
    """Describe a single tool parameter for prompt generation and validation."""

    def __init__(
        self,
        name: str,
        type: str,
        description: str,
        required: bool = True,
        default=None,
    ):
        self.name = name
        self.type = type
        self.description = description
        self.required = required
        self.default = default


class Tool:
    """Thin wrapper around a callable — no business logic, just dispatch."""

    def __init__(
        self,
        name: str,
        description: str,
        func: callable,
        parameters: list[ToolParameter],
    ):
        self.name = name
        self.description = description
        self.func = func
        self.parameters = parameters

    def run(self, params: dict) -> str:
        """Validate params and call the underlying function. Returns result string."""
        kwargs = {}
        for p in self.parameters:
            if p.name in params:
                kwargs[p.name] = params[p.name]
            elif p.required and p.default is None:
                raise ValueError(
                    f"Tool '{self.name}': missing required parameter '{p.name}'"
                )
            elif not p.required:
                kwargs[p.name] = p.default
        return self.func(**kwargs)

    def to_prompt_desc(self) -> str:
        """Generate a human-readable tool description for the system prompt."""
        param_strs = []
        for p in self.parameters:
            opt = " (可选)" if not p.required else ""
            param_strs.append(f"{p.name}: {p.type}{opt}")
        return f"- {self.name}({', '.join(param_strs)}): {self.description}"

    def to_openai_schema(self) -> dict:
        """[Reserved] Convert to OpenAI function calling format."""
        raise NotImplementedError("to_openai_schema() not yet implemented")

    def to_langchain_tool(self):
        """Convert this Tool to a LangChain StructuredTool for use with
        ToolNode and bind_tools()."""
        from langchain_core.tools import StructuredTool

        # Dynamically build args_schema Pydantic model
        fields = {}
        for p in self.parameters:
            py_type = _PARAM_TYPE_MAP.get(p.type, str)
            if p.required:
                fields[p.name] = (py_type, ...)
            else:
                fields[p.name] = (py_type, p.default)

        schema = create_model(f"{self.name}_args", **fields)

        import inspect
        _sig = inspect.signature(self.func)

        def _func(**kwargs):
            filtered = {k: v for k, v in kwargs.items() if k in _sig.parameters}
            return self.func(**filtered)

        return StructuredTool(
            name=self.name,
            description=self.description,
            func=_func,
            args_schema=schema,
        )
