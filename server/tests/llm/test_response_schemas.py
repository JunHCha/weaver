"""Discover every concrete generate_json schema, including future call sites."""

import ast
import importlib
from pathlib import Path

import anthropic
import pytest
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2] / "src" / "weaver"


def response_schemas(source, module):
    tree = ast.parse(source)
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            continue
        if call.func.attr != "generate_json":
            continue
        schema = next((kw.value for kw in call.keywords if kw.arg == "schema"), None)
        if schema is None:
            assert len(call.args) == 4, f"unrecognized schema call in {module.__name__}"
            schema = call.args[3]
        assert isinstance(schema, ast.Name), f"unresolved schema in {module.__name__}"
        model = getattr(module, schema.id, None)
        if isinstance(model, type) and issubclass(model, BaseModel):
            yield model
            continue
        # Generic client decorators forward the caller's schema unchanged.
        owner = parents[call]
        while not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = parents[owner]
        parameter = next((arg for arg in owner.args.args if arg.arg == schema.id), None)
        assert parameter is not None and parameter.annotation is not None, (
            f"unresolved schema {schema.id} in {module.__name__}"
        )
        assert ast.unparse(parameter.annotation) == "type[T]"
        assert not any(
            isinstance(node, ast.Name)
            and node.id == schema.id
            and isinstance(node.ctx, ast.Store)
            for node in ast.walk(owner)
        ), "forwarded schema was reassigned"


def discovered_schemas():
    models = set()
    for path in sorted(ROOT.rglob("*.py")):
        source = path.read_text()
        if "generate_json" not in source:
            continue
        relative = path.relative_to(ROOT.parent).with_suffix("")
        module = importlib.import_module(".".join(relative.parts))
        models.update(response_schemas(source, module))
    assert models, "schema discovery found no generate_json calls"
    return sorted(models, key=lambda model: (model.__module__, model.__name__))


@pytest.mark.parametrize(
    "model", discovered_schemas(), ids=lambda model: model.__name__
)
def test_response_schema_transforms(model):
    assert anthropic.transform_schema(model)["type"] == "object"


def test_discovery_rejects_unknown_schema():
    module = importlib.import_module("weaver.loom.summary_review")
    with pytest.raises(AssertionError, match="unresolved schema"):
        list(
            response_schemas(
                "def task():\n    llm.generate_json('task', '', '', MissingModel)",
                module,
            )
        )


def test_discovery_covers_new_call_without_a_model_list():
    module = importlib.import_module("weaver.loom.summary_review")
    assert list(
        response_schemas(
            "llm.generate_json('new-task', '', '', ApprovedResponse)", module
        )
    ) == [module.ApprovedResponse]
    assert list(
        response_schemas(
            "llm.generate_json(task='new-task', system='', user='', schema=FeedbackResponse)",
            module,
        )
    ) == [module.FeedbackResponse]
