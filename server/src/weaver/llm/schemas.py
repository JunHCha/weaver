"""OpenAI strict wire schemas; original models remain the validation authority."""

from typing import Any

from pydantic import BaseModel

# Unsupported validation constraints are enforced after generation by Pydantic.
_UNSUPPORTED = {
    "not",
    "if",
    "then",
    "else",
    "default",
    "discriminator",
    "format",
    "minLength",
    "maxLength",
    "pattern",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "maxProperties",
    "propertyNames",
    "patternProperties",
    "unevaluatedProperties",
    "$schema",
}


def openai_schema(model: type[BaseModel]) -> dict[str, Any]:
    def visit(node: Any) -> Any:
        if isinstance(node, list):
            return [visit(item) for item in node]
        if not isinstance(node, dict):
            return node
        result = {}
        for key, value in node.items():
            if key in _UNSUPPORTED:
                continue
            # OpenAI accepts anyOf; Pydantic's discriminated unions use oneOf.
            result["anyOf" if key == "oneOf" else key] = (
                {name: visit(item) for name, item in value.items()}
                if key in {"properties", "$defs"}
                else visit(value)
            )
        if result.get("type") == "object" or "properties" in result:
            properties = result.setdefault("properties", {})
            required = node.get("required", [])
            for name, prop in properties.items():
                if name not in required:
                    properties[name] = {"anyOf": [prop, {"type": "null"}]}
            result["required"] = list(properties)
            result["additionalProperties"] = False
        return result

    return visit(model.model_json_schema())  # type: ignore[no-any-return]


def restore_omitted_defaults(value: Any, model: type[BaseModel]) -> Any:
    """Map strict-output null sentinels back to omitted, non-nullable defaults.

    OpenAI requires every property even when the original model allows omission.
    Actual nullable fields keep their null value; only added nulls are removed.
    """
    original = model.model_json_schema()
    definitions = original.get("$defs", {})

    def nullable(node: dict[str, Any]) -> bool:
        return node.get("type") == "null" or any(
            nullable(branch) for branch in node.get("anyOf", [])
        )

    def visit(data: Any, node: dict[str, Any]) -> Any:
        if "$ref" in node:
            return visit(data, definitions[node["$ref"].split("/")[-1]])
        for branch in node.get("anyOf", node.get("oneOf", [])):
            data = visit(data, branch)
        if isinstance(data, list) and "items" in node:
            return [visit(item, node["items"]) for item in data]
        if isinstance(data, dict) and "properties" in node:
            result = dict(data)
            for name, prop in node["properties"].items():
                if name not in result:
                    continue
                if (
                    result[name] is None
                    and name not in node.get("required", [])
                    and not nullable(prop)
                ):
                    del result[name]
                else:
                    result[name] = visit(result[name], prop)
            return result
        return data

    return visit(value, original)
