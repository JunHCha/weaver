package io.github.junhcha.weaver.courier

import org.junit.Assert.*
import org.junit.Test

class CourierRulesTest {
    @Test fun namesAreFailClosed() {
        assertTrue(CourierRules.ignored(".syncthing.a.tmp"))
        assertTrue(CourierRules.ignored("~syncthing~a"))
        assertTrue(CourierRules.conflict("a.sync-conflict-20261107.json"))
        listOf("../a", "a/b", "a\\b", ".", "..", "").forEach {
            assertFalse(CourierRules.safeName(it))
        }
    }

    @Test fun onlyOldPhoneTempsCanBeRemoved() {
        val id = "cap_01ARZ3NDEKTSV4RRFFQ69G5FAV"
        assertTrue(CourierRules.ownTemporary(".$id.txt.0123456789abcdef.tmp"))
        assertFalse(CourierRules.ownTemporary(".syncthing.a.tmp"))
        assertFalse(CourierRules.ownTemporary(".stfolder"))
        assertFalse(CourierRules.ownTemporary(".arbitrary.0123456789abcdef.tmp"))
        assertTrue(CourierRules.phoneTemporary("decisions", ".pkg_0123456789abcdef.r2.json.0123456789abcdef.tmp"))
        assertFalse(CourierRules.phoneTemporary("decisions", ".pkg_0123456789abcdef.json.0123456789abcdef.tmp"))
    }

    @Test fun reviewFileNamesCarryOpaquePackageIds() {
        val match = CourierRules.revision.matchEntire("pkg_0123456789abcdef.r12.json")!!
        assertEquals(listOf("pkg_0123456789abcdef", "12"), match.groupValues.drop(1))
        assertNull(CourierRules.revision.matchEntire("pkg_0123456789abcdef.r0.json"))
        assertNull(CourierRules.revision.matchEntire("../pkg.r1.json"))
        assertNull(CourierRules.revision.matchEntire("pkg.r1.json.tmp"))
    }
}
