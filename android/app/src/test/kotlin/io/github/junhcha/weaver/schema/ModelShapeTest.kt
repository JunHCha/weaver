package io.github.junhcha.weaver.schema

import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.descriptors.PrimitiveKind
import kotlinx.serialization.descriptors.SerialDescriptor
import kotlinx.serialization.descriptors.SerialKind
import kotlinx.serialization.descriptors.StructureKind
import kotlinx.serialization.descriptors.elementNames
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The hand-written Kotlin models must have exactly the shape of each schema's top-level
 * `properties` and the definitions they reference: same keys, all required, same
 * nullability, same enum values and compatible primitive types. A schema change that is
 * not mirrored in Kotlin fails here before any fixture is read.
 */
@OptIn(ExperimentalSerializationApi::class)
class ModelShapeTest {
    @Test
    fun kotlinModelsMirrorSchemaProperties() {
        SharedSchema.models.forEach { (model, serializer) ->
            val root = SharedSchema.schemaJson(model)
            Walker(root["\$defs"]?.jsonObject ?: JsonObject(emptyMap()))
                .compare(model, root, serializer.descriptor)
        }
    }

    private class Walker(private val defs: JsonObject) {
        fun compare(path: String, node: JsonObject, descriptor: SerialDescriptor) {
            if (descriptor.serialName.startsWith("kotlinx.serialization.json.JsonElement")) {
                assertTrue("$path: JsonElement must map to an unconstrained schema", resolve(node).isEmpty())
                return
            }
            val branches = nonNullBranches(node)
            if (branches.isEmpty()) {
                assertTrue("$path: null-only schema must be a nullable Kotlin type", descriptor.isNullable)
                return
            }
            when (val kind = descriptor.kind) {
                StructureKind.CLASS -> compareObject(path, single(path, branches), descriptor)
                StructureKind.LIST -> {
                    val array = single(path, branches)
                    assertEquals("$path: type", "array", array["type"]?.jsonPrimitive?.content)
                    compare("$path[]", array.getValue("items").jsonObject, descriptor.getElementDescriptor(0))
                }
                SerialKind.ENUM -> {
                    val values = single(path, branches).getValue("enum").jsonArray.map { it.jsonPrimitive.content }
                    assertEquals("$path: enum values", values.toSet(), descriptor.elementNames.toSet())
                }
                StructureKind.MAP -> {
                    val map = single(path, branches)
                    assertEquals("$path: type", "object", map["type"]?.jsonPrimitive?.content)
                    compare("$path{key}", map.getValue("propertyNames").jsonObject, descriptor.getElementDescriptor(0))
                    compare("$path{value}", map.getValue("additionalProperties").jsonObject, descriptor.getElementDescriptor(1))
                }
                is PrimitiveKind -> {
                    val expected = when (kind) {
                        PrimitiveKind.STRING -> "string"
                        PrimitiveKind.BOOLEAN -> "boolean"
                        PrimitiveKind.INT, PrimitiveKind.LONG -> "integer"
                        PrimitiveKind.DOUBLE -> "number"
                        else -> error("$path: unexpected primitive $kind")
                    }
                    branches.forEach { assertEquals("$path: type", expected, it["type"]?.jsonPrimitive?.content) }
                }
                else -> error("$path: unsupported descriptor kind $kind")
            }
        }

        private fun compareObject(path: String, node: JsonObject, descriptor: SerialDescriptor) {
            assertEquals("$path: additionalProperties", "false", node["additionalProperties"]?.jsonPrimitive?.content)
            val properties = node.getValue("properties").jsonObject
            val required = node.getValue("required").jsonArray.map { it.jsonPrimitive.content }.toSet()
            assertEquals("$path: property names", properties.keys, descriptor.elementNames.toSet())
            // Only keys the server defaults (and so omits from `required`) may have a Kotlin default.
            val optional = (0 until descriptor.elementsCount).filter { descriptor.isElementOptional(it) }
                .map { descriptor.getElementName(it) }.toSet()
            assertEquals("$path: every key is required except defaulted ones", properties.keys - required, optional)
            assertTrue("$path: required keys are declared", properties.keys.containsAll(required))
            for (i in 0 until descriptor.elementsCount) {
                val name = descriptor.getElementName(i)
                val child = properties.getValue(name).jsonObject
                val element = descriptor.getElementDescriptor(i)
                assertEquals("$path.$name: nullability", isNullable(child), element.isNullable)
                compare("$path.$name", child, element)
            }
        }

        private fun single(path: String, branches: List<JsonObject>): JsonObject {
            assertEquals("$path: expected one non-null schema branch", 1, branches.size)
            return branches.single()
        }

        private fun resolve(node: JsonObject): JsonObject {
            val ref = node["\$ref"]?.jsonPrimitive?.content ?: return node
            return resolve(defs.getValue(ref.removePrefix("#/\$defs/")).jsonObject)
        }

        private fun isNullable(node: JsonObject): Boolean {
            val resolved = resolve(node)
            if (resolved["type"] == JsonPrimitive("null")) return true
            return resolved["anyOf"]?.jsonArray?.any { isNullable(it.jsonObject) } == true
        }

        /** Flattens $ref/anyOf into the non-null alternatives (e.g. ent_ ID | ref | "this"). */
        private fun nonNullBranches(node: JsonObject): List<JsonObject> {
            val resolved = resolve(node)
            if (resolved["type"] == JsonPrimitive("null")) return emptyList()
            // An object's own anyOf is a constraint, not a union.
            if ("type" in resolved || "properties" in resolved) return listOf(resolved)
            val anyOf = resolved["anyOf"] as? JsonArray ?: return listOf(resolved)
            return anyOf.flatMap { nonNullBranches(it.jsonObject) }
        }
    }
}
