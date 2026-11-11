package io.github.junhcha.weaver.pipeline

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.PathParser
import androidx.compose.ui.unit.dp

/**
 * Material Icons paths, Apache 2.0. Vectors avoid baseline differences and scale without a font.
 */
object PipelineIcons {
    private val paths =
        mapOf(
            MapNode.SHEPHERD to
                "M19 8h-1V5h-3v3H9V5H6v3H5c-1.1 0-2 .9-2 2v5h1.17c.41-1.17 1.52-2 2.83-2s2.42.83 2.83 2H15v-5h4v5h2v-5c0-1.1-.9-2-2-2zM7 14c-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3-1.34-3-3-3zm0 4.5c-.83 0-1.5-.67-1.5-1.5s.67-1.5 1.5-1.5 1.5.67 1.5 1.5-.67 1.5-1.5 1.5zM18 14c-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3-1.34-3-3-3zm0 4.5c-.83 0-1.5-.67-1.5-1.5s.67-1.5 1.5-1.5 1.5.67 1.5 1.5-.67 1.5-1.5 1.5z",
            MapNode.SPINNER to
                "M12 22c4.97 0 9-4.03 9-9 0-3.87-3.13-7-7-7-2.76 0-5 2.24-5 5 0 1.66 1.34 3 3 3 .55 0 1-.45 1-1s-.45-1-1-1-1-.45-1-1c0-1.66 1.34-3 3-3 2.76 0 5 2.24 5 5 0 3.87-3.13 7-7 7-4.97 0-9-4.03-9-9 0-3.87 3.13-7 7-7h2V2h-2C5.03 2 1 6.03 1 11c0 6.08 4.92 11 11 11z",
            MapNode.USER to
                "M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z",
            MapNode.LOOM to
                "M3 3v18h18V3H3zm6 16H5v-4h4v4zm0-6H5V9h4v4zm0-6H5V5h4v2zm6 12h-4v-4h4v4zm0-6h-4V9h4v4zm0-6h-4V5h4v2zm4 12h-2v-4h2v4zm0-6h-2V9h2v4zm0-6h-2V5h2v2z",
            MapNode.FABRIC to
                "M19 3H5v18l3-2 4 2 4-2 3 2V3zm-3 13H8v-2h8v2zm0-4H8v-2h8v2zm0-4H8V6h8v2z",
            MapNode.WIKI to
                "M21 4c-1.11-.35-2.33-.5-3.5-.5-1.95 0-4.05.4-5.5 1.5-1.45-1.1-3.55-1.5-5.5-1.5S2.45 3.9 1 5v14.5c0 .25.25.5.5.5.1 0 .15-.05.25-.05C3.1 19.3 5.05 19 6.5 19c1.95 0 4.05.4 5.5 1.5 1.35-.85 3.8-1.5 5.5-1.5 1.65 0 3.35.3 4.75.95.1.05.15.05.25.05.25 0 .5-.25.5-.5V5c-.6-.45-1.25-.75-2-1zm0 13.5c-1.1-.35-2.3-.5-3.5-.5-1.7 0-4.15.65-5.5 1.5v-11C13.35 6.65 15.8 6 17.5 6c1.2 0 2.4.15 3.5.5v11z",
            MapNode.COURIER to
                "M20 8h-3V4H3c-1.1 0-2 .9-2 2v11h2c0 1.66 1.34 3 3 3s3-1.34 3-3h6c0 1.66 1.34 3 3 3s3-1.34 3-3h2v-5l-3-4zM6 18.5c-.83 0-1.5-.67-1.5-1.5s.67-1.5 1.5-1.5 1.5.67 1.5 1.5-.67 1.5-1.5 1.5zM17 9.5h2.5l1.96 2.5H17V9.5zm1 9c-.83 0-1.5-.67-1.5-1.5s.67-1.5 1.5-1.5 1.5.67 1.5 1.5-.67 1.5-1.5 1.5z",
        )
    private val cache = mutableMapOf<MapNode, ImageVector>()

    fun get(node: MapNode): ImageVector =
        cache.getOrPut(node) {
            ImageVector.Builder(node.name, 24.dp, 24.dp, 24f, 24f)
                .addPath(
                    PathParser().parsePathString(paths.getValue(node)).toNodes(),
                    fill = SolidColor(Color.Black),
                )
                .build()
        }
}
