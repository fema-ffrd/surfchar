
"""Functions for creating and analyzing breaklines."""

from osgeo import ogr
import pandas as pd
import os

from surfchar.utils import (
    remove_output,
)


def buffer_polygons(
    input_path: str,
    output_path: str,
    distance: float,
) -> None:
    """
    Buffer polygon features using OGR while preserving attributes.
    """
    remove_output(output_path)

    src_ds = ogr.Open(input_path)
    if src_ds is None:
        raise ValueError(
            f"Could not open input vector: {input_path}"
        )

    src_layer = src_ds.GetLayer()
    src_defn = src_layer.GetLayerDefn()

    output_ext = os.path.splitext(output_path)[1].lower()

    if output_ext == ".shp":
        driver = ogr.GetDriverByName("ESRI Shapefile")
    elif output_ext == ".gpkg":
        driver = ogr.GetDriverByName("GPKG")
    else:
        src_ds = None
        raise ValueError(
            f"Unsupported output format: {output_ext}"
        )

    out_ds = driver.CreateDataSource(output_path)
    if out_ds is None:
        src_ds = None
        raise ValueError(
            f"Could not create output vector: {output_path}"
        )

    output_layer_name = os.path.splitext(
        os.path.basename(output_path)
    )[0]

    out_layer = out_ds.CreateLayer(
        output_layer_name,
        srs=src_layer.GetSpatialRef(),
        geom_type=ogr.wkbPolygon,
    )

    # Preserve all source fields, including gridcode.
    for field_index in range(src_defn.GetFieldCount()):
        field_definition = src_defn.GetFieldDefn(field_index)
        out_layer.CreateField(field_definition)

    out_defn = out_layer.GetLayerDefn()

    for source_feature in src_layer:
        source_geometry = source_feature.GetGeometryRef()

        if source_geometry is None:
            continue

        buffered_geometry = source_geometry.Buffer(
            float(distance)
        )

        if (
            buffered_geometry is None
            or buffered_geometry.IsEmpty()
        ):
            continue

        output_feature = ogr.Feature(out_defn)
        output_feature.SetGeometry(buffered_geometry)

        for field_index in range(src_defn.GetFieldCount()):
            field_name = (
                src_defn
                .GetFieldDefn(field_index)
                .GetNameRef()
            )

            output_feature.SetField(
                field_name,
                source_feature.GetField(field_name),
            )

        out_layer.CreateFeature(output_feature)

        output_feature = None
        buffered_geometry = None

    out_ds = None
    src_ds = None


def polygons_to_lines(
    input_path: str,
    output_path: str,
) -> None:
    """
    Convert polygon boundaries to single-part line features using OGR only.
    """
    remove_output(output_path)

    src_ds = ogr.Open(input_path)
    if src_ds is None:
        raise ValueError(
            f"Could not open input vector: {input_path}"
        )

    src_layer = src_ds.GetLayer()
    src_defn = src_layer.GetLayerDefn()

    driver = ogr.GetDriverByName(
        "ESRI Shapefile"
    )

    out_ds = driver.CreateDataSource(
        output_path
    )

    if out_ds is None:
        src_ds = None
        raise ValueError(
            f"Could not create output: {output_path}"
        )

    layer_name = os.path.splitext(
        os.path.basename(output_path)
    )[0]

    out_layer = out_ds.CreateLayer(
        layer_name,
        srs=src_layer.GetSpatialRef(),
        geom_type=ogr.wkbLineString,
    )

    for field_index in range(
        src_defn.GetFieldCount()
    ):
        out_layer.CreateField(
            src_defn.GetFieldDefn(field_index)
        )

    out_defn = out_layer.GetLayerDefn()

    for source_feature in src_layer:
        source_geometry = (
            source_feature.GetGeometryRef()
        )

        if source_geometry is None:
            continue

        boundary = source_geometry.Boundary()

        for line_part in iter_line_parts(boundary):
            output_feature = ogr.Feature(
                out_defn
            )

            output_feature.SetGeometry(
                line_part.Clone()
            )

            for field_index in range(
                src_defn.GetFieldCount()
            ):
                field_name = (
                    src_defn
                    .GetFieldDefn(field_index)
                    .GetNameRef()
                )

                output_feature.SetField(
                    field_name,
                    source_feature.GetField(
                        field_name
                    ),
                )

            out_layer.CreateFeature(
                output_feature
            )

            output_feature = None

        boundary = None

    out_ds = None
    src_ds = None


def iter_line_parts(geometry):
    """
    Yield individual LineString parts from an OGR geometry.

    Intersection operations may return a LineString,
    MultiLineString, or GeometryCollection.
    """
    if geometry is None or geometry.IsEmpty():
        return

    flat_type = ogr.GT_Flatten(
        geometry.GetGeometryType()
    )

    if flat_type in (
        ogr.wkbLineString,
        ogr.wkbLinearRing,
    ):
        yield geometry
        return

    if flat_type in (
        ogr.wkbMultiLineString,
        ogr.wkbGeometryCollection,
    ):
        for index in range(
            geometry.GetGeometryCount()
        ):
            part = geometry.GetGeometryRef(index)

            if part is not None:
                yield from iter_line_parts(part)
                
                
def extend_breakline_segment(
    watershed_line,
    segment,
    buffer_length: float,
):
    """
    Extend a short clipped segment using the original watershed line.

    Logical equivalent of the ArcPy workflow:

    1. Buffer the short segment.
    2. Clip the original watershed line with that buffer.
    3. If the result is multipart, retain the longest part.
    """
    if (
        watershed_line is None
        or watershed_line.IsEmpty()
        or segment is None
        or segment.IsEmpty()
    ):
        return None

    segment_buffer = segment.Buffer(
        float(buffer_length)
    )

    if (
        segment_buffer is None
        or segment_buffer.IsEmpty()
    ):
        return None

    extended_geometry = watershed_line.Intersection(
        segment_buffer
    )

    segment_buffer = None

    if (
        extended_geometry is None
        or extended_geometry.IsEmpty()
    ):
        return None

    extended_parts = [
        part.Clone()
        for part in iter_line_parts(extended_geometry)
        if part is not None and not part.IsEmpty()
    ]

    extended_geometry = None

    if not extended_parts:
        return None

    # ArcPy retained the longest line when extension
    # produced a multipart result.
    return max(
        extended_parts,
        key=lambda geometry: geometry.Length(),
    )
    

def clip_watershed(
    watershed_line,
    clip_polygon,
    min_breakline_length: float,
) -> list:
    """
    Clip one watershed line and reproduce the original ArcPy logic.

    Short clipped parts are extended along the original watershed
    line before they are discarded.
    """
    if (
        watershed_line is None
        or watershed_line.IsEmpty()
        or clip_polygon is None
        or clip_polygon.IsEmpty()
    ):
        return []

    initial_clip = watershed_line.Intersection(
        clip_polygon
    )

    if (
        initial_clip is None
        or initial_clip.IsEmpty()
    ):
        return []

    clipped_parts = [
        part.Clone()
        for part in iter_line_parts(initial_clip)
        if part is not None and not part.IsEmpty()
    ]

    initial_clip = None

    if not clipped_parts:
        return []

    output_lines = []

    for clipped_part in clipped_parts:
        if (
            clipped_part.Length()
            >= float(min_breakline_length)
        ):
            output_lines.append(clipped_part)
            continue

        extended_line = extend_breakline_segment(
            watershed_line=watershed_line,
            segment=clipped_part,
            buffer_length=min_breakline_length,
        )

        if extended_line is None:
            continue

        # Match the ArcPy straggler rejection.
        # If extension still cannot produce the requested
        # minimum length, omit the tiny segment.
        if (
            extended_line.Length()
            < float(min_breakline_length)
        ):
            continue

        output_lines.append(extended_line)

    return output_lines


def clip_watersheds(
    watersheds: str,
    sinks_buffer: str,
    sinks_that_overflow: pd.Series,
    watersheds_clipped: str,
    watershed_to_sink: dict[int, int],
    watersheds_fill_buffer: str | None = None,
    clip_all: bool = False,
    min_breakline_length: float = 200.0,
) -> None:
    """
    Clip watershed lines using sink-buffer polygons with OGR only.

    If watersheds_fill_buffer is provided, the larger polygon for each
    gridcode is used as the clipping polygon.

    If clip_all is False:
        - overflowing watershed lines are clipped
        - non-overflowing watershed lines are written unchanged

    If clip_all is True:
        - all watershed lines are clipped

    Clipped line parts shorter than min_breakline_length are extended
    along the original watershed line. Parts are discarded only when
    the extended result remains shorter than min_breakline_length.
    """

    remove_output(watersheds_clipped)

    overflow_ids = {
        int(value)
        for value in sinks_that_overflow.dropna().astype(int).tolist()
    }
    
    #debug 
    print("Overflow IDs:", list(overflow_ids)[:20])

    # ---------------------------------------------------------
    # Load the normal sink buffers into a gridcode lookup.
    # ---------------------------------------------------------
    sinks_buffer_ds = ogr.Open(sinks_buffer)
    if sinks_buffer_ds is None:
        raise ValueError(
            f"Could not open sink-buffer vector: {sinks_buffer}"
        )

    sinks_buffer_layer = sinks_buffer_ds.GetLayer()

    buffer_lookup = {}

    for feature in sinks_buffer_layer:
        sink_id = feature.GetField("gridcode")
        geometry = feature.GetGeometryRef()

        if sink_id is None or geometry is None:
            continue

        buffer_lookup[int(sink_id)] = geometry.Clone()
    
    # debug    
    print("Buffer IDs:",list(buffer_lookup.keys())[:20])

    sinks_buffer_ds = None

    # ---------------------------------------------------------
    # If stage-storage buffers exist, use the larger buffer.
    # ---------------------------------------------------------
    if watersheds_fill_buffer:
        fill_buffer_ds = ogr.Open(watersheds_fill_buffer)
        if fill_buffer_ds is None:
            raise ValueError(
                f"Could not open watershed-fill buffer: "
                f"{watersheds_fill_buffer}"
            )

        fill_buffer_layer = fill_buffer_ds.GetLayer()
        
        for feature in fill_buffer_layer:
            fill_gridcode = feature.GetField("gridcode")
            fill_geometry = feature.GetGeometryRef()

            if (
                fill_gridcode is None
                or fill_geometry is None
            ):
                continue

            fill_gridcode = int(fill_gridcode)

            # The GDAL stage-storage polygons may be keyed by
            # watershed ID. Resolve that value back to the sink ID.
            #
            # If the value is already a sink ID, retain it.
            sink_id = watershed_to_sink.get(
                fill_gridcode,
                fill_gridcode,
            )

            if sink_id not in buffer_lookup:
                raise ValueError(
                    "No normal sink buffer found for "
                    f"stage-storage gridcode {fill_gridcode}; "
                    f"resolved sink ID was {sink_id}"
                )

            normal_area = buffer_lookup[sink_id].GetArea()
            fill_area = fill_geometry.GetArea()

            if fill_area > normal_area:
                buffer_lookup[sink_id] = fill_geometry.Clone()

        fill_buffer_ds = None

    # ---------------------------------------------------------
    # Open the watershed line layer.
    # ---------------------------------------------------------
    watersheds_ds = ogr.Open(watersheds)
    if watersheds_ds is None:
        raise ValueError(
            f"Could not open watershed lines: {watersheds}"
        )

    watersheds_layer = watersheds_ds.GetLayer()

    # ---------------------------------------------------------
    # Create the output shapefile.
    # ---------------------------------------------------------
    output_driver = ogr.GetDriverByName("ESRI Shapefile")

    output_ds = output_driver.CreateDataSource(watersheds_clipped)
    if output_ds is None:
        watersheds_ds = None
        raise ValueError(
            f"Could not create output: {watersheds_clipped}"
        )

    output_layer_name = os.path.splitext(
        os.path.basename(watersheds_clipped)
    )[0]

    output_layer = output_ds.CreateLayer(
        output_layer_name,
        srs=watersheds_layer.GetSpatialRef(),
        geom_type=ogr.wkbLineString,
    )

    output_layer.CreateField(
        ogr.FieldDefn("gridcode", ogr.OFTInteger)
    )

    output_definition = output_layer.GetLayerDefn()

    # ---------------------------------------------------------
    # Helper to write one line feature.
    # ---------------------------------------------------------
    def write_line(watershed_id: int, geometry) -> None:
        if geometry is None or geometry.IsEmpty():
            return

        output_feature = ogr.Feature(output_definition)
        output_feature.SetField("gridcode", int(watershed_id))
        output_feature.SetGeometry(geometry.Clone())

        output_layer.CreateFeature(output_feature)

        output_feature = None

    # ---------------------------------------------------------
    # Process watershed lines one feature at a time.
    # ---------------------------------------------------------
    for feature in watersheds_layer:
        watershed_id = feature.GetField("gridcode")
        watershed_geometry = feature.GetGeometryRef()

        if watershed_id is None or watershed_geometry is None:
            continue
        
        watershed_id = int(watershed_id)

        # The watershed-line gridcode is a GDAL watershed value.
        # Resolve it to the sink that created that watershed.
        sink_id = watershed_to_sink.get(watershed_id)

        if sink_id is None:
            print(
                f"Skipping watershed gridcode "
                f"{watershed_id} "
                f"(no sink mapping)"
            )
            continue

        should_clip = (
            clip_all
            or sink_id in overflow_ids
        )

        if not should_clip:
            # ArcPy wrote non-overflow watershed lines unchanged
            # whenever clip_all was False.
            for line_part in iter_line_parts(
                watershed_geometry
            ):
                write_line(
                    watershed_id=watershed_id,
                    geometry=line_part,
                )
            continue
        
        print(
            "Processing watershed:",
            watershed_id
        )

        clip_geometry = buffer_lookup.get(sink_id)
        
        if clip_geometry is None:
            raise ValueError(
                f"No clipping buffer found for sink ID {sink_id}, "
                f"associated with watershed ID {watershed_id}"
            )
        
        #debug
        if sink_id in overflow_ids:
            print(
                f"watershed={watershed_id}",
                f"clip found={clip_geometry is not None}"
            )

        clipped_lines = clip_watershed(
            watershed_line=watershed_geometry,
            clip_polygon=clip_geometry,
            min_breakline_length=min_breakline_length,
        )

        print(
            f"watershed_id={watershed_id}, "
            f"sink_id={sink_id}, "
            f"clipped_parts={len(clipped_lines)}"
        )

        for clipped_line in clipped_lines:
            write_line(
                watershed_id=watershed_id,
                geometry=clipped_line,
            )

    output_ds = None
    watersheds_ds = None
    

def dissolve_breaklines(
    watersheds_lines_clipped: str,
) -> list:
    """
    Dissolve breaklines using OGR only.
    """

    ds = ogr.Open(watersheds_lines_clipped)

    if ds is None:
        raise ValueError(
            f"Could not open {watersheds_lines_clipped}"
        )

    layer = ds.GetLayer()

    merged = None

    for feat in layer:

        geom = feat.GetGeometryRef()

        if geom is None:
            continue

        geom = geom.Clone()

        if merged is None:
            merged = geom

        else:
            merged = merged.Union(geom)

    ds = None

    if merged is None:
        return []

    breaklines = []

    gtype = ogr.GT_Flatten(
        merged.GetGeometryType()
    )

    if gtype == ogr.wkbLineString:

        breaklines.append(
            merged.Clone()
        )

    elif gtype == ogr.wkbMultiLineString:

        for i in range(
            merged.GetGeometryCount()
        ):
            breaklines.append(
                merged.GetGeometryRef(i).Clone()
            )

    return breaklines


def fix_self_closing_breaklines(
    breaklines: list,
) -> list:
    """
    Split self-closing OGR LineStrings into two line features.

    Parameters
    ----------
    breaklines : list
        List of OGR LineString geometries.

    Returns
    -------
    list
        List of non-self-closing OGR LineString geometries.
    """
    fixed_breaklines = []

    for line in breaklines:

        if line is None or line.IsEmpty():
            continue

        geometry_type = ogr.GT_Flatten(
            line.GetGeometryType()
        )

        if geometry_type != ogr.wkbLineString:
            continue

        point_count = line.GetPointCount()

        if point_count < 4:
            fixed_breaklines.append(
                line.Clone()
            )
            continue

        first_point = line.GetPoint(0)
        last_point = line.GetPoint(
            point_count - 1
        )

        first_x = first_point[0]
        first_y = first_point[1]

        last_x = last_point[0]
        last_y = last_point[1]

        is_closed = (
            first_x == last_x
            and first_y == last_y
        )

        if not is_closed:
            fixed_breaklines.append(
                line.Clone()
            )
            continue

        midpoint = point_count // 2

        first_line = ogr.Geometry(
            ogr.wkbLineString
        )

        for point_index in range(
            0,
            midpoint + 1,
        ):
            point = line.GetPoint(point_index)
            first_line.AddPoint(*point)

        second_line = ogr.Geometry(
            ogr.wkbLineString
        )

        for point_index in range(
            midpoint,
            point_count,
        ):
            point = line.GetPoint(point_index)
            second_line.AddPoint(*point)

        if first_line.GetPointCount() >= 2:
            fixed_breaklines.append(
                first_line
            )

        if second_line.GetPointCount() >= 2:
            fixed_breaklines.append(
                second_line
            )

    return fixed_breaklines


def write_breaklines_shapefile(
    breaklines: list,
    shapefile_path: str,
    srs=None,
) -> None:
    """
    Write OGR LineString breaklines to a shapefile.
    """
    remove_output(shapefile_path)

    driver = ogr.GetDriverByName("ESRI Shapefile")

    out_ds = driver.CreateDataSource(shapefile_path)
    if out_ds is None:
        raise ValueError(
            f"Could not create output shapefile: "
            f"{shapefile_path}"
        )

    layer_name = os.path.splitext(
        os.path.basename(shapefile_path)
    )[0]

    out_layer = out_ds.CreateLayer(
        layer_name,
        srs=srs,
        geom_type=ogr.wkbLineString,
    )

    length_field = ogr.FieldDefn(
        "length_ft",
        ogr.OFTReal,
    )
    out_layer.CreateField(length_field)

    layer_defn = out_layer.GetLayerDefn()

    for line in breaklines:
        if line is None or line.IsEmpty():
            continue

        geometry_type = ogr.GT_Flatten(
            line.GetGeometryType()
        )

        if geometry_type != ogr.wkbLineString:
            continue

        feature = ogr.Feature(layer_defn)
        feature.SetGeometry(line.Clone())
        feature.SetField(
            "length_ft",
            float(line.Length()),
        )

        out_layer.CreateFeature(feature)
        feature = None

    out_ds = None