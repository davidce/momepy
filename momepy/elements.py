#!/usr/bin/env python
# -*- coding: utf-8 -*-

# elements.py
# generating derived elements (street edge, block)
import geopandas as gpd
import pandas as pd
from tqdm import tqdm  # progress bar
import math
import rtree
from osgeo import ogr
from shapely.wkt import loads
import numpy as np
from scipy.spatial import Voronoi
from shapely.geometry import Point, LineString, Polygon, MultiPolygon
import shapely
import operator
from libpysal.weights import Queen


def buffered_limit(gdf, buffer=100):
    """
    Define limit for tessellation as a buffer around buildings.

    Parameters
    ----------
    gdf : GeoDataFrame
        GeoDataFrame containing building footprints
    buffer : float
        buffer around buildings limiting the extend of tessellation

    Returns
    -------
    MultiPolygon
        MultiPolygon or Polygon defining the study area

    """
    study_area = gdf.copy()
    study_area['geometry'] = study_area.buffer(buffer)
    study_area['diss'] = 1
    built_up_df = study_area.dissolve(by='diss')
    built_up = built_up_df.geometry[1]
    return built_up


def _get_centre(gdf):
    """
    Returns centre coords of gdf.
    """
    bounds = gdf['geometry'].bounds
    centre_x = (bounds['maxx'].max() + bounds['minx'].min()) / 2
    centre_y = (bounds['maxy'].max() + bounds['miny'].min()) / 2
    return centre_x, centre_y


# densify geometry before Voronoi tesselation
def _densify(geom, segment):
    """
    Returns densified geoemtry with segments no longer than `segment`.
    """
    poly = geom
    wkt = geom.wkt  # shapely Polygon to wkt
    geom = ogr.CreateGeometryFromWkt(wkt)  # create ogr geometry
    geom.Segmentize(segment)  # densify geometry by 2 metres
    geom.CloseRings()  # fix for GDAL 2.4.1 bug
    wkt2 = geom.ExportToWkt()  # ogr geometry to wkt
    try:
        new = loads(wkt2)  # wkt to shapely Polygon
        return new
    except:
        return poly


def _point_array(objects, unique_id):
    """
    Returns lists of points and ids based on geometry and unique_id.
    """
    points = []
    ids = []
    for idx, row in tqdm(objects.iterrows(), total=objects.shape[0]):
        poly_ext = row['geometry'].boundary
        if poly_ext is not None:
            if poly_ext.type == 'MultiLineString':
                for line in poly_ext:
                    point_coords = line.coords
                    row_array = np.array(point_coords).tolist()
                    for i in range(len(row_array)):
                        points.append(row_array[i])
                        ids.append(row[unique_id])
            elif poly_ext.type == 'LineString':
                point_coords = poly_ext.coords
                row_array = np.array(point_coords).tolist()
                for i in range(len(row_array)):
                    points.append(row_array[i])
                    ids.append(row[unique_id])
            else:
                raise Exception('Boundary type is {}'.format(poly_ext.type))
    return points, ids


def _regions(voronoi_diagram, unique_id, ids, crs):
    """
    Generate GeoDataFrame of Voronoi regions from scipy.spatial.Voronoi.
    """
    # generate DataFrame of results
    regions = pd.DataFrame()
    regions[unique_id] = ids  # add unique id
    regions['region'] = voronoi_diagram.point_region  # add region id for each point

    # add vertices of each polygon
    vertices = []
    for region in regions.region:
        vertices.append(voronoi_diagram.regions[region])
    regions['vertices'] = vertices

    # convert vertices to Polygons
    polygons = []
    for region in tqdm(regions.vertices, desc='Vertices to Polygons'):
        if -1 not in region:
            polygons.append(Polygon(voronoi_diagram.vertices[region]))
        else:
            polygons.append(None)
    # save polygons as geometry column
    regions['geometry'] = polygons

    # generate GeoDataFrame
    regions_gdf = gpd.GeoDataFrame(regions.dropna(), geometry='geometry')
    regions_gdf = regions_gdf.loc[regions_gdf['geometry'].length < 1000000]  # delete errors
    regions_gdf = regions_gdf.loc[regions_gdf[unique_id] != -1]  # delete hull-based cells
    regions_gdf.crs = crs
    return regions_gdf


def _split_lines(polygon, distance, crs):
    dense = _densify(polygon, distance)
    boundary = dense.boundary

    def _pair(coords):
        '''Iterate over pairs in a list -> pair of points '''
        for i in range(1, len(coords)):
            yield coords[i - 1], coords[i]

    segments = []
    if boundary.type == 'LineString':
        for seg_start, seg_end in _pair(boundary.coords):
            segment = LineString([seg_start, seg_end])
            segments.append(segment)
    elif boundary.type == 'MultiLineString':
        for ls in boundary:
            for seg_start, seg_end in _pair(ls.coords):
                segment = LineString([seg_start, seg_end])
                segments.append(segment)

    cutted = gpd.GeoSeries(segments, crs=crs)
    return cutted


def _cut(tessellation, limit, unique_id):
    """
    Cut tessellation by the limit (Multi)Polygon.

    ADD: add option to delete everything outside of limit. Now it keeps it.
    """
    # cut infinity of voronoi by set buffer (thanks for script to Geoff Boeing)
    print('Preparing buffer zone for edge resolving...')
    geometry_cut = _split_lines(limit, 100, tessellation.crs)

    print('Building R-tree...')
    sindex = tessellation.sindex
    # find the points that intersect with each subpolygon and add them to points_within_geometry
    to_cut = pd.DataFrame()
    for poly in geometry_cut:
        # find approximate matches with r-tree, then precise matches from those approximate ones
        possible_matches_index = list(sindex.intersection(poly.bounds))
        possible_matches = tessellation.iloc[possible_matches_index]
        precise_matches = possible_matches[possible_matches.intersects(poly)]
        to_cut = to_cut.append(precise_matches)

    # delete duplicates
    to_cut = to_cut.drop_duplicates(subset=[unique_id])
    subselection = list(to_cut.index)

    print('Cutting...')
    for idx, row in tqdm(tessellation.loc[subselection].iterrows(), total=tessellation.loc[subselection].shape[0]):
        intersection = row.geometry.intersection(limit)
        if intersection.type == 'MultiPolygon':
            areas = {}
            for p in range(len(intersection)):
                area = intersection[p].area
                areas[p] = area
            maximal = max(areas.items(), key=operator.itemgetter(1))[0]
            tessellation.loc[idx, 'geometry'] = intersection[maximal]
        elif intersection.type == 'GeometryCollection':
            for geom in list(intersection.geoms):
                if geom.type != 'Polygon':
                    pass
                else:
                    tessellation.loc[idx, 'geometry'] = geom
        else:
            tessellation.loc[idx, 'geometry'] = intersection
    return tessellation, sindex


def _check_result(tesselation, orig_gdf, unique_id):
    """
    Check whether result of tessellation matches buildings and contains only Polygons.
    """
    # check against input layer
    ids_original = list(orig_gdf[unique_id])
    ids_generated = list(tesselation[unique_id])
    if len(ids_original) != len(ids_generated):
        import warnings
        diff = set(ids_original).difference(ids_generated)
        warnings.warn("Tessellation does not fully match buildings. {len} element(s) collapsed "
                      "during generation - unique_id: {i}".format(len=len(diff), i=diff))

    # check MultiPolygons - usually caused by error in input geometry
    uids = tesselation[tesselation.geometry.type == 'MultiPolygon'][unique_id]
    if len(uids) > 0:
        import warnings
        warnings.warn('Tessellation contains MultiPolygon elements. Initial objects should be edited. '
                      'unique_id of affected elements: {}'.format(list(uids)))


def _queen_corners(tessellation, sensitivity, sindex):
    """
    Experimental: Fix unprecise corners.
    """
    changes = {}
    qid = 0

    for ix, row in tqdm(tessellation.iterrows(), total=tessellation.shape[0]):
        corners = []
        change = []

        cell = row.geometry
        coords = cell.exterior.coords
        for i in coords:
            point = Point(i)
            possible_matches_index = list(sindex.intersection(point.bounds))
            possible_matches = tessellation.iloc[possible_matches_index]
            precise_matches = sum(possible_matches.intersects(point))
            if precise_matches > 2:
                corners.append(point)

        if len(corners) > 2:
            for c in range(len(corners)):
                next_c = c + 1
                if c == (len(corners) - 1):
                    next_c = 0
                if corners[c].distance(corners[next_c]) < sensitivity:
                    change.append([corners[c], corners[next_c]])
        elif len(corners) == 2:
            if corners[0].distance(corners[1]) > 0:
                if corners[0].distance(corners[1]) < sensitivity:
                    change.append([corners[0], corners[1]])

        if change:
            for points in change:
                x_new = np.mean([points[0].x, points[1].x])
                y_new = np.mean([points[0].y, points[1].y])
                new = [(x_new, y_new), id]
                changes[(points[0].x, points[0].y)] = new
                changes[(points[1].x, points[1].y)] = new
                qid = qid + 1

    for ix, row in tqdm(tessellation.iterrows(), total=tessellation.shape[0]):
        cell = row.geometry
        coords = list(cell.exterior.coords)

        moves = {}
        for x in coords:
            if x in changes.keys():
                moves[coords.index(x)] = changes[x]
        keys = list(moves.keys())
        delete_points = []
        for move in range(len(keys)):
            if move < len(keys) - 1:
                if moves[keys[move]][1] == moves[keys[move + 1]][1] and keys[move + 1] - keys[move] < 5:
                    delete_points = delete_points + (coords[keys[move]:keys[move + 1]])
                    # change the code above to have if based on distance not number

        newcoords = [changes[x][0] if x in changes.keys() else x for x in coords]
        for coord in newcoords:
            if coord in delete_points:
                newcoords.remove(coord)
        if coords != newcoords:
            if not cell.interiors:
                # newgeom = Polygon(newcoords).buffer(0)
                be = Polygon(newcoords).exterior
                mls = be.intersection(be)
                if len(list(shapely.ops.polygonize(mls))) > 1:
                    newgeom = MultiPolygon(shapely.ops.polygonize(mls))
                    geoms = []
                    for g in range(len(newgeom)):
                        geoms.append(newgeom[g].area)
                    newgeom = newgeom[geoms.index(max(geoms))]
                else:
                    newgeom = list(shapely.ops.polygonize(mls))[0]
            else:
                newgeom = Polygon(newcoords, holes=cell.interiors)
            tessellation.loc[ix, 'geometry'] = newgeom
    return tessellation


def tessellation(gdf, unique_id, limit, shrink=0.4, segment=0.5, queen_corners=False, sensitivity=2):
    """
    Generate morphological tessellation around given buildings.

    Parameters
    ----------
    gdf : GeoDataFrame
        GeoDataFrame containing building footprints
    unique_id : str
        name of the column with unique id
    limit : MultiPolygon or Polygon
        MultiPolygon or Polygon defining the study area limiting tessellation (otherwise it could go to infinity).
    shrink : float (default 0.4)
        distance for negative buffer to generate space between adjacent buildings.
    segment : float (default 0.5)
        maximum distance between points on Polygon after discretisation

    Returns
    -------
    GeoDataFrame
        GeoDataFrame of morphological tessellation with the unique id based on original buildings.

    Notes
    -------
    queen_corners and sensitivity are currently experimental only and can cause errors.
    """
    objects = gdf.copy()

    centre = _get_centre(objects)
    objects['geometry'] = objects['geometry'].translate(xoff=-centre[0], yoff=-centre[1])

    print('Bufferring geometry...')
    objects['geometry'] = objects.geometry.apply(lambda g: g.buffer(-shrink, cap_style=2, join_style=2))

    print('Converting multipart geometry to singlepart...')
    objects = objects.explode()
    objects.reset_index(inplace=True, drop=True)

    print('Densifying geometry...')
    objects['geometry'] = objects['geometry'].apply(_densify, segment=segment)

    print('Generating input point array...')
    points, ids = _point_array(objects, unique_id)

    # add convex hull buffered large distance to eliminate infinity issues
    series = gpd.GeoSeries(limit, crs=gdf.crs).translate(xoff=-centre[0], yoff=-centre[1])
    hull = series.geometry[0].convex_hull.buffer(300)
    hull = _densify(hull, 20)
    hull_array = np.array(hull.boundary.coords).tolist()
    for i in range(len(hull_array)):
        points.append(hull_array[i])
        ids.append(-1)

    print('Generating Voronoi diagram...')
    voronoi_diagram = Voronoi(np.array(points))

    print('Generating GeoDataFrame...')
    regions_gdf = _regions(voronoi_diagram, unique_id, ids, crs=gdf.crs)

    print('Dissolving Voronoi polygons...')
    morphological_tessellation = regions_gdf[[unique_id, 'geometry']].dissolve(by=unique_id, as_index=False)

    morphological_tessellation['geometry'] = morphological_tessellation['geometry'].translate(xoff=centre[0], yoff=centre[1])

    morphological_tessellation, sindex = _cut(morphological_tessellation, limit, unique_id)

    if queen_corners is True:
        morphological_tessellation = _queen_corners(morphological_tessellation, sensitivity, sindex)

    _check_result(morphological_tessellation, gdf, unique_id=unique_id)

    return morphological_tessellation


def snap_street_network_edge(edges, buildings, tessellation, tolerance_street, tolerance_edge):
    """
    Fix street network before performing blocks()

    Extends unjoined ends of street segments to join with other segmets or tessellation boundary.

    Parameters
    ----------
    edges : GeoDataFrame
        GeoDataFrame containing street network
    buildings : GeoDataFrame
        GeoDataFrame containing building footprints
    tessellation : GeoDataFrame
        GeoDataFrame containing morphological tessellation
    tolerance_street : float
        tolerance in snapping to street network (by how much could be street segment extended).
    tolerance_edge : float
        tolerance in snapping to edge of tessellated area (by how much could be street segment extended).

    Returns
    -------
    GeoDataFrame
        GeoDataFrame of extended street network.

    """
    # extrapolating function - makes line as a extrapolation of existing with set length (tolerance)
    def getExtrapoledLine(p1, p2, tolerance):
        """
        Creates a line extrapoled in p1->p2 direction.
        """
        EXTRAPOL_RATIO = tolerance  # length of a line
        a = p2

        # defining new point based on the vector between existing points
        if p1[0] >= p2[0] and p1[1] >= p2[1]:
            b = (p2[0] - EXTRAPOL_RATIO * math.cos(math.atan(math.fabs(p1[1] - p2[1] + 0.000001) / math.fabs(p1[0] - p2[0] + 0.000001))),
                 p2[1] - EXTRAPOL_RATIO * math.sin(math.atan(math.fabs(p1[1] - p2[1] + 0.000001) / math.fabs(p1[0] - p2[0] + 0.000001))))
        elif p1[0] <= p2[0] and p1[1] >= p2[1]:
            b = (p2[0] + EXTRAPOL_RATIO * math.cos(math.atan(math.fabs(p1[1] - p2[1] + 0.000001) / math.fabs(p1[0] - p2[0] + 0.000001))),
                 p2[1] - EXTRAPOL_RATIO * math.sin(math.atan(math.fabs(p1[1] - p2[1] + 0.000001) / math.fabs(p1[0] - p2[0] + 0.000001))))
        elif p1[0] <= p2[0] and p1[1] <= p2[1]:
            b = (p2[0] + EXTRAPOL_RATIO * math.cos(math.atan(math.fabs(p1[1] - p2[1] + 0.000001) / math.fabs(p1[0] - p2[0] + 0.000001))),
                 p2[1] + EXTRAPOL_RATIO * math.sin(math.atan(math.fabs(p1[1] - p2[1] + 0.000001) / math.fabs(p1[0] - p2[0] + 0.000001))))
        else:
            b = (p2[0] - EXTRAPOL_RATIO * math.cos(math.atan(math.fabs(p1[1] - p2[1] + 0.000001) / math.fabs(p1[0] - p2[0] + 0.000001))),
                 p2[1] + EXTRAPOL_RATIO * math.sin(math.atan(math.fabs(p1[1] - p2[1] + 0.000001) / math.fabs(p1[0] - p2[0] + 0.000001))))
        return LineString([a, b])

    # function extending line to closest object within set distance
    def extend_line(tolerance, idx):
        """
        Extends a line geometry withing GeoDataFrame to snap on itself withing tolerance.
        """
        if Point(l_coords[-2]).distance(Point(l_coords[-1])) <= 0.001:
            if len(l_coords) > 2:
                extra = l_coords[-3:-1]
            else:
                return False
        else:
            extra = l_coords[-2:]
        extrapolation = getExtrapoledLine(*extra, tolerance=tolerance)  # we use the last two points

        possible_intersections_index = list(sindex.intersection(extrapolation.bounds))
        possible_intersections_lines = network.iloc[possible_intersections_index]
        possible_intersections_clean = possible_intersections_lines.drop(idx, axis=0)
        possible_intersections = possible_intersections_clean.intersection(extrapolation)

        if possible_intersections.any():

            true_int = []
            for one in list(possible_intersections.index):
                if possible_intersections[one].type == 'Point':
                    true_int.append(possible_intersections[one])
                elif possible_intersections[one].type == 'MultiPoint':
                    true_int.append(possible_intersections[one][0])
                    true_int.append(possible_intersections[one][1])

            if len(true_int) >= 1:
                if len(true_int) > 1:
                    distances = {}
                    ix = 0
                    for p in true_int:
                        distance = p.distance(Point(l_coords[-1]))
                        distances[ix] = distance
                        ix = ix + 1
                    minimal = min(distances.items(), key=operator.itemgetter(1))[0]
                    new_point_coords = true_int[minimal].coords[0]
                else:
                    new_point_coords = true_int[0].coords[0]

                l_coords.append(new_point_coords)
                new_extended_line = LineString(l_coords)

                # check whether the line goes through buildings. if so, ignore it
                possible_buildings_index = list(bindex.intersection(new_extended_line.bounds))
                possible_buildings = buildings.iloc[possible_buildings_index]
                possible_intersections = possible_buildings.intersection(new_extended_line)

                if possible_intersections.any():
                    pass
                else:
                    network.loc[idx, 'geometry'] = new_extended_line
        else:
            return False

    # function extending line to closest object within set distance to edge defined by tessellation
    def extend_line_edge(tolerance, idx):
        """
        Extends a line geometry withing GeoDataFrame to snap on the boundary of tessellation withing tolerance.
        """
        if Point(l_coords[-2]).distance(Point(l_coords[-1])) <= 0.001:
            if len(l_coords) > 2:
                extra = l_coords[-3:-1]
            else:
                return False
        else:
            extra = l_coords[-2:]
        extrapolation = getExtrapoledLine(*extra, tolerance)  # we use the last two points

        # possible_intersections_index = list(qindex.intersection(extrapolation.bounds))
        # possible_intersections_lines = geometry_cut.iloc[possible_intersections_index]
        possible_intersections = geometry.intersection(extrapolation)

        if possible_intersections.type != 'GeometryCollection':

            true_int = []

            if possible_intersections.type == 'Point':
                true_int.append(possible_intersections)
            elif possible_intersections.type == 'MultiPoint':
                true_int.append(possible_intersections[0])
                true_int.append(possible_intersections[1])

            if len(true_int) >= 1:
                if len(true_int) > 1:
                    distances = {}
                    ix = 0
                    for p in true_int:
                        distance = p.distance(Point(l_coords[-1]))
                        distances[ix] = distance
                        ix = ix + 1
                    minimal = min(distances.items(), key=operator.itemgetter(1))[0]
                    new_point_coords = true_int[minimal].coords[0]
                else:
                    new_point_coords = true_int[0].coords[0]

                l_coords.append(new_point_coords)
                new_extended_line = LineString(l_coords)

                # check whether the line goes through buildings. if so, ignore it
                possible_buildings_index = list(bindex.intersection(new_extended_line.bounds))
                possible_buildings = buildings.iloc[possible_buildings_index]
                possible_intersections = possible_buildings.intersection(new_extended_line)

                if possible_intersections.any():
                    pass
                else:
                    network.loc[idx, 'geometry'] = new_extended_line

    network = edges.copy()
    # generating spatial index (rtree)
    print('Building R-tree for network...')
    sindex = network.sindex
    print('Building R-tree for buildings...')
    bindex = buildings.sindex
    print('Dissolving tesselation...')
    geometry = tessellation.geometry.unary_union.boundary

    print('Snapping...')
    # iterating over each street segment
    for idx, row in tqdm(network.iterrows(), total=network.shape[0]):

        line = row['geometry']
        l_coords = list(line.coords)
        # network_w = network.drop(idx, axis=0)['geometry']  # ensure that it wont intersect itself
        start = Point(l_coords[0])
        end = Point(l_coords[-1])

        # find out whether ends of the line are connected or not
        possible_first_index = list(sindex.intersection(start.bounds))
        possible_first_matches = network.iloc[possible_first_index]
        possible_first_matches_clean = possible_first_matches.drop(idx, axis=0)
        first = possible_first_matches_clean.intersects(start).any()

        possible_second_index = list(sindex.intersection(end.bounds))
        possible_second_matches = network.iloc[possible_second_index]
        possible_second_matches_clean = possible_second_matches.drop(idx, axis=0)
        second = possible_second_matches_clean.intersects(end).any()

        # both ends connected, do nothing
        if first and second:
            continue
        # start connected, extend  end
        elif first and not second:
            if extend_line(tolerance_street, idx) is False:
                extend_line_edge(tolerance_edge, idx)
        # end connected, extend start
        elif not first and second:
            l_coords.reverse()
            if extend_line(tolerance_street, idx) is False:
                extend_line_edge(tolerance_edge, idx)
        # unconnected, extend both ends
        elif not first and not second:
            if extend_line(tolerance_street, idx) is False:
                extend_line_edge(tolerance_edge, idx)
            l_coords.reverse()
            if extend_line(tolerance_street, idx) is False:
                extend_line_edge(tolerance_edge, idx)
        else:
            print('Something went wrong.')

    return network


def blocks(tessellation, edges, buildings, id_name, unique_id):
    """
    Generate blocks based on buildings, tesselation and street network

    Adds bID to buildings and tesselation.

    Parameters
    ----------
    tessellation : GeoDataFrame
        GeoDataFrame containing morphological tessellation
    edges : GeoDataFrame
        GeoDataFrame containing street network
    buildings : GeoDataFrame
        GeoDataFrame containing buildings
    id_name : str
        name of the unique blocks id column to be generated
    unique_id : str
        name of the column with unique id. If there is none, it could be generated by unique_id().
        This should be the same for cells and buildings, id's should match.

    Returns
    -------
    buildings, cells, blocks : tuple

    buildings : GeoDataFrame
        GeoDataFrame containing buildings with added block ID
    cells : GeoDataFrame
        GeoDataFrame containing morphological tessellation with added block ID
    blocks : GeoDataFrame
        GeoDataFrame containing generated blocks
    """

    cells_copy = tessellation.copy()

    print('Buffering streets...')
    street_buff = edges.copy()
    street_buff['geometry'] = street_buff.buffer(0.1)

    print('Generating spatial index...')
    streets_index = street_buff.sindex

    print('Difference...')
    cells_geom = cells_copy.geometry
    new_geom = []

    for ix, cell in tqdm(cells_geom.iteritems(), total=cells_geom.shape[0]):
        # find approximate matches with r-tree, then precise matches from those approximate ones
        possible_matches_index = list(streets_index.intersection(cell.bounds))
        possible_matches = street_buff.iloc[possible_matches_index]
        new_geom.append(cell.difference(possible_matches.geometry.unary_union))

    single_geom = []
    print('Defining adjacency...')
    for p in new_geom:
        if p.type == 'MultiPolygon':
            for polygon in p:
                single_geom.append(polygon)
        else:
            single_geom.append(p)

    blocks_gdf = gpd.GeoDataFrame(geometry=gpd.GeoSeries(single_geom))
    spatial_weights = Queen.from_dataframe(blocks_gdf, silence_warnings=True)

    patches = {}
    jID = 1
    for idx, row in tqdm(blocks_gdf.iterrows(), total=blocks_gdf.shape[0]):

        # if the id is already present in courtyards, continue (avoid repetition)
        if idx in patches:
            continue
        else:
            to_join = [idx]  # list of indices which should be joined together
            neighbours = []  # list of neighbours
            weights = spatial_weights.neighbors[idx]  # neighbours from spatial weights
            for w in weights:
                neighbours.append(w)  # make a list from weigths

            for n in neighbours:
                while n not in to_join:  # until there is some neighbour which is not in to_join
                    to_join.append(n)
                    weights = spatial_weights.neighbors[n]
                    for w in weights:
                        neighbours.append(w)  # extend neighbours by neighbours of neighbours :)
            for b in to_join:
                patches[b] = jID  # fill dict with values
            jID = jID + 1

    blocks_gdf['patch'] = blocks_gdf.index.map(patches)

    print('Defining street-based blocks...')
    blocks_single = blocks_gdf.dissolve(by='patch')

    blocks_single['geometry'] = blocks_single.buffer(0.1)

    print('Defining block ID...')  # street based
    blocks_single[id_name] = None
    blocks_single[id_name] = blocks_single[id_name].astype('float')
    b_id = 1
    for idx, row in tqdm(blocks_single.iterrows(), total=blocks_single.shape[0]):
        blocks_single.loc[idx, id_name] = b_id
        b_id = b_id + 1

    print('Generating centroids...')
    buildings_c = buildings.copy()
    buildings_c['geometry'] = buildings_c.representative_point()  # make centroids
    blocks_single.crs = buildings.crs

    print('Spatial join...')
    centroids_tempID = gpd.sjoin(buildings_c, blocks_single, how='left', op='intersects')

    tempID_to_uID = centroids_tempID[[unique_id, id_name]]

    print('Attribute join (tesselation)...')
    cells_copy = cells_copy.merge(tempID_to_uID, on=unique_id)

    print('Generating blocks...')
    blocks = cells_copy.dissolve(by=id_name)
    cells_copy = cells_copy.drop([id_name], axis=1)

    print('Multipart to singlepart...')
    blocks = blocks.explode()
    blocks.reset_index(inplace=True, drop=True)

    blocks['geometry'] = blocks.exterior

    uid = 1
    for idx, row in tqdm(blocks.iterrows(), total=blocks.shape[0]):
        blocks.loc[idx, id_name] = uid
        uid = uid + 1
        blocks.loc[idx, 'geometry'] = Polygon(row['geometry'])

    # if polygon is within another one, delete it
    sindex = blocks.sindex
    for idx, row in tqdm(blocks.iterrows(), total=blocks.shape[0]):
        possible_matches = list(sindex.intersection(row.geometry.bounds))
        possible_matches.remove(idx)
        possible = blocks.iloc[possible_matches]

        for idx2, row2 in possible.iterrows():
            if row['geometry'].within(row2['geometry']):
                blocks.loc[idx, 'delete'] = 1

    if 'delete' in blocks.columns:
        blocks = blocks.drop(list(blocks.loc[blocks['delete'] == 1].index))

    blocks_save = blocks[[id_name, 'geometry']]

    centroids_w_bl_ID2 = gpd.sjoin(buildings_c, blocks_save, how='left', op='intersects')
    bl_ID_to_uID = centroids_w_bl_ID2[[unique_id, id_name]]

    print('Attribute join (buildings)...')
    buildings = buildings.merge(bl_ID_to_uID, on=unique_id)

    print('Attribute join (tesselation)...')
    cells = tessellation.merge(bl_ID_to_uID, on=unique_id)

    print('Done')
    return (buildings, cells, blocks_save)


def get_network_id(left, right, unique_id, network_id, min_size=100):
    """
    Snap each element (preferably building) to the closest street network segment, saves its id.

    Adds network ID to elements.

    Parameters
    ----------
    left : GeoDataFrame
        GeoDataFrame containing objects to snap
    right : GeoDataFrame
        GeoDataFrame containing street network with unique network ID.
        If there is none, it could be generated by :py:func:`momepy.elements.unique_id`.
    unique_id : str, list, np.array, pd.Series (default None)
        the name of the elements dataframe column, np.array, or pd.Series with unique id
    network_id : str, list, np.array, pd.Series (default None)
        the name of the streets dataframe column, np.array, or pd.Series with network unique id.
    min_size : int (default 100)
        min_size should be a vaule such that if you build a box centered in each
        building centroid with edges of size `2*min_size`, you know a priori that at least one
        segment is intersected with the box.

    Returns
    -------
    elements_nID : Series
        Series containing network ID for elements

    Examples
    --------
    >>> buildings_df['nID'] = momepy.get_network_id(buildings_df, streets_df, 'uID', 'nID')
    Generating centroids...
    Generating list of points...
    100%|██████████| 144/144 [00:00<00:00, 4273.75it/s]
    Generating list of lines...
    100%|██████████| 33/33 [00:00<00:00, 3594.37it/s]
    Generating rtree...
    100%|██████████| 33/33 [00:00<00:00, 6291.74it/s]
    Snapping...
    100%|██████████| 144/144 [00:00<00:00, 2718.98it/s]
    Forming DataFrame...
    Joining DataFrames...
    Cleaning DataFrames...
    Merging with elements...
    Done.
    >>> buildings_df['nID'][0]
    1
    """
    INFTY = 1000000000000
    MIN_SIZE = min_size
    # MIN_SIZE should be a vaule such that if you build a box centered in each
    # point with edges of size 2*MIN_SIZE, you know a priori that at least one
    # segment is intersected with the box. Otherwise, you could get an inexact
    # solution, there is an exception checking this, though.

    def distance(a, b):
        return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2)

    def get_distance(apoint, segment):
        a = apoint
        b, c = segment
        # t = <a-b, c-b>/|c-b|**2
        # because p(a) = t*(c-b)+b is the ortogonal projection of vector a
        # over the rectline that includes the points b and c.
        t = (a[0] - b[0]) * (c[0] - b[0]) + (a[1] - b[1]) * (c[1] - b[1])
        t = t / ((c[0] - b[0]) ** 2 + (c[1] - b[1]) ** 2)
        # Only if t 0 <= t <= 1 the projection is in the interior of
        # segment b-c, and it is the point that minimize the distance
        # (by pythagoras theorem).
        if 0 < t < 1:
            pcoords = (t * (c[0] - b[0]) + b[0], t * (c[1] - b[1]) + b[1])
            dmin = distance(a, pcoords)
            return pcoords, dmin
        elif t <= 0:
            return b, distance(a, b)
        return c, distance(a, c)

    def get_rtree(lines):
        def generate_items():
            sindx = 0
            for nid, l in tqdm(lines, total=len(lines)):
                for i in range(len(l) - 1):
                    a, b = l[i]
                    c, d = l[i + 1]
                    segment = ((a, b), (c, d))
                    box = (min(a, c), min(b, d), max(a, c), max(b, d))
                    # box = left, bottom, right, top
                    yield (sindx, box, (segment, nid))
                    sindx += 1
        return rtree.index.Index(generate_items())

    def get_solution(idx, points):
        result = {}
        for p in tqdm(points, total=len(points)):
            pbox = (p[0] - MIN_SIZE, p[1] - MIN_SIZE, p[0] + MIN_SIZE, p[1] + MIN_SIZE)
            hits = idx.intersection(pbox, objects='raw')
            d = INFTY
            s = None
            for h in hits:
                nearest_p, new_d = get_distance(p, h[0])
                if d >= new_d:
                    d = new_d
                    # s = (h[0], h[1], nearest_p, new_d)
                    s = (h[0], h[1], h[-1])
            result[p] = s
            if s is None:
                result[p] = (0, 0)

        return result

    if not isinstance(unique_id, str):
        left['mm_uid'] = unique_id
        unique_id = 'mm_uid'
    if not isinstance(network_id, str):
        right['mm_nid'] = network_id
        network_id = 'mm_nid'

    print('Generating centroids...')
    buildings_c = left.copy()
    if network_id in buildings_c.columns:
        buildings_c = buildings_c.drop([network_id], axis=1)

    buildings_c['geometry'] = buildings_c.centroid  # make centroids

    print('Generating list of points...')
    # make points list for input
    centroid_list = []
    for idx, row in tqdm(buildings_c.iterrows(), total=buildings_c.shape[0]):
        centroid_list = centroid_list + list(row['geometry'].coords)

    print('Generating list of lines...')
    # make streets list for input
    street_list = []
    for idx, row in tqdm(right.iterrows(), total=right.shape[0]):
        street_list.append((row[network_id], list(row['geometry'].coords)))
    print('Generating rtree...')
    idx = get_rtree(street_list)

    print('Snapping...')
    solutions = get_solution(idx, centroid_list)

    print('Forming DataFrame...')
    df = pd.DataFrame.from_dict(solutions, orient='index', columns=['unused', 'unused', network_id])  # solutions dict to df
    df['point'] = df.index  # point to column
    df = df.reset_index()
    df['idx'] = df.index
    buildings_c['idx'] = buildings_c.index

    print('Joining DataFrames...')
    joined = buildings_c.merge(df, on='idx')
    print('Cleaning DataFrames...')
    cleaned = joined[[unique_id, network_id]]

    print('Merging with objects...')
    if network_id in left.columns:
        elements_copy = left.copy().drop([network_id], axis=1)
        elements_m = elements_copy.merge(cleaned, on=unique_id)
    else:
        elements_m = left.merge(cleaned, on=unique_id)

    if elements_m[network_id].isnull().any():
        import warnings
        warnings.warn('Some objects were not attached to the network. '
                      'Set larger min_size. {} affected elements'.format(sum(elements_m[network_id].isnull())))

    if 'mm_uid' in left.columns:
        left.drop(columns=['mm_uid'], inplace=True)
    if 'mm_nid' in right.columns:
        right.drop(columns=['mm_nid'], inplace=True)
    return elements_m[network_id]


def get_node_id(objects, nodes, edges, node_id, edge_id):
    """
    Snap each building to closest street network node on the closest network edge.

    Adds node ID to objects (preferably buildings). Gets ID of edge, and determines
    which of its end points is closer.

    Parameters
    ----------
    objects : GeoDataFrame
        GeoDataFrame containing objects to snap
    nodes : GeoDataFrame
        GeoDataFrame containing street nodes with unique node ID.
        If there is none, it could be generated by :py:func:`momepy.unique_id`.
    edges : GeoDataFrame
        GeoDataFrame containing street edges with unique edge ID and IDs of start
        and end points of each segment. Start and endpoints are default outcome of :py:func:`momepy.nx_to_gdf`.
    node_id : str, list, np.array, pd.Series (default None)
        the name of the nodes dataframe column, np.array, or pd.Series with unique id

    Returns
    -------
    node_ids : Series
        Series containing node ID for objects

    Examples
    --------

    """
    if not isinstance(node_id, str):
        nodes['mm_noid'] = node_id
        node_id = 'mm_noid'

    results_list = []
    for index, row in tqdm(objects.iterrows(), total=objects.shape[0]):
        if np.isnan(row[edge_id]):

            results_list.append(np.nan)
        else:
            centroid = row.geometry.centroid
            edge = edges.loc[edges[edge_id] == row[edge_id]].iloc[0]
            startID = edge.node_start
            start = nodes.loc[nodes[node_id] == startID].iloc[0].geometry
            sd = centroid.distance(start)
            endID = edge.node_end
            end = nodes.loc[nodes[node_id] == endID].iloc[0].geometry
            ed = centroid.distance(end)
            if sd > ed:
                results_list.append(endID)
            else:
                results_list.append(startID)

    series = pd.Series(results_list)
    return series


# '''
# street_edges():
#
# Generate street edges based on buildings, blocks, tesselation and street network with street names
# Adds nID and eID to buildings and tesselation.
#
#     buildings = gdf of buildings (with unique id)
#     streets = gdf of street network (with street names and unique network segment id)
#     tesselation = gdf of tesselation (with unique id and block id)
#     street_name_column = column with street names
#     unique_id_column = column with unique ids
#     block_id_column = column with block ids
#     network_id_column = column with network ids
#     tesselation_to = path to save tesselation with nID, eID
#     buildings_to = path to save buildings with nID, eID
#     save_to = path to save street edges
#
# Optional:
# '''
#
#
# def street_edges(buildings, streets, tesselation, street_name_column,
#                  unique_id_column, block_id_column, network_id_column,
#                  tesselation_to, buildings_to, save_to):
#     INFTY = 1000000000000
#     MIN_SIZE = 100
#     # MIN_SIZE should be a vaule such that if you build a box centered in each
#     # point with edges of size 2*MIN_SIZE, you know a priori that at least one
#     # segment is intersected with the box. Otherwise, you could get an inexact
#     # solution, there is an exception checking this, though.
#
#     def distance(a, b):
#         return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2)
#
#     def get_distance(apoint, segment):
#         a = apoint
#         b, c = segment
#         # t = <a-b, c-b>/|c-b|**2
#         # because p(a) = t*(c-b)+b is the ortogonal projection of vector a
#         # over the rectline that includes the points b and c.
#         t = (a[0] - b[0]) * (c[0] - b[0]) + (a[1] - b[1]) * (c[1] - b[1])
#         t = t / ((c[0] - b[0]) ** 2 + (c[1] - b[1]) ** 2)
#         # Only if t 0 <= t <= 1 the projection is in the interior of
#         # segment b-c, and it is the point that minimize the distance
#         # (by pythagoras theorem).
#         if 0 < t < 1:
#             pcoords = (t * (c[0] - b[0]) + b[0], t * (c[1] - b[1]) + b[1])
#             dmin = distance(a, pcoords)
#             return pcoords, dmin
#         elif t <= 0:
#             return b, distance(a, b)
#         elif 1 <= t:
#             return c, distance(a, c)
#
#     def get_rtree(lines):
#         def generate_items():
#             sindx = 0
#             for lid, nid, l in tqdm(lines, total=len(lines)):
#                 for i in range(len(l) - 1):
#                     a, b = l[i]
#                     c, d = l[i + 1]
#                     segment = ((a, b), (c, d))
#                     box = (min(a, c), min(b, d), max(a, c), max(b, d))
#                     # box = left, bottom, right, top
#                     yield (sindx, box, (lid, segment, nid))
#                     sindx += 1
#         return index.Index(generate_items())
#
#     def get_solution(idx, points):
#         result = {}
#         for p in tqdm(points, total=len(points)):
#             pbox = (p[0] - MIN_SIZE, p[1] - MIN_SIZE, p[0] + MIN_SIZE, p[1] + MIN_SIZE)
#             hits = idx.intersection(pbox, objects='raw')
#             d = INFTY
#             s = None
#             for h in hits:
#                 nearest_p, new_d = get_distance(p, h[1])
#                 if d >= new_d:
#                     d = new_d
#                     # s = (h[0], h[1], nearest_p, new_d)
#                     s = (h[0], h[1], h[-1])
#             result[p] = s
#             if s is None:
#                 result[p] = (0, 0)
#
#             # some checking you could remove after you adjust the constants
#             # if s is None:
#             #     raise Warning("It seems INFTY is not big enough. Point was not attached to street. It might be too far.", p)
#
#             # pboxpol = ((pbox[0], pbox[1]), (pbox[2], pbox[1]),
#             #            (pbox[2], pbox[3]), (pbox[0], pbox[3]))
#             # if not Polygon(pboxpol).intersects(LineString(s[1])):
#             #     msg = "It seems MIN_SIZE is not big enough. "
#             #     msg += "You could get inexact solutions if remove this exception."
#             #     raise Exception(msg)
#
#         return result
#
#     print('Generating centroids...')
#     buildings_c = buildings.copy()
#     buildings_c['geometry'] = buildings_c.centroid  # make centroids
#
#     print('Generating list of points...')
#     # make points list for input
#     centroid_list = []
#     for idx, row in tqdm(buildings_c.iterrows(), total=buildings_c.shape[0]):
#         centroid_list = centroid_list + list(row['geometry'].coords)
#
#     print('Generating list of lines...')
#     # make streets list for input
#     street_list = []
#     for idx, row in tqdm(streets.iterrows(), total=streets.shape[0]):
#         street_list.append((row[street_name_column], row[network_id_column], list(row['geometry'].coords)))
#     print('Generating rtree...')
#     idx = get_rtree(street_list)
#
#     print('Snapping...')
#     solutions = get_solution(idx, centroid_list)
#
#     print('Forming DataFrame...')
#     df = pd.DataFrame.from_dict(solutions, orient='index', columns=['street', 'unused', network_id_column])  # solutions dict to df
#     df['point'] = df.index  # point to column
#     df = df.reset_index()
#     df['idx'] = df.index
#     buildings_c['idx'] = buildings_c.index
#
#     print('Joining DataFrames...')
#     joined = buildings_c.merge(df, on='idx')
#     print('Cleaning DataFrames...')
#     cleaned = joined[[unique_id_column, 'street', network_id_column]]
#
#     print('Merging with tesselation...')
#     tesselation = tesselation.merge(cleaned, on=unique_id_column)
#
#     print('Defining merge ID...')
#     for idx, row in tqdm(tesselation.iterrows(), total=tesselation.shape[0]):
#         tesselation.loc[idx, 'mergeID'] = str(row['street']) + str(row[block_id_column])
#
#     print('Dissolving...')
#     edges = tesselation.dissolve(by='mergeID')
#
#     # multipart geometry to singlepart
#     def multi2single(gpdf):
#         gpdf_singlepoly = gpdf[gpdf.geometry.type == 'Polygon']
#         gpdf_multipoly = gpdf[gpdf.geometry.type == 'MultiPolygon']
#
#         for i, row in gpdf_multipoly.iterrows():
#             Series_geometries = pd.Series(row.geometry)
#             df = pd.concat([gpd.GeoDataFrame(row, crs=gpdf_multipoly.crs).T] * len(Series_geometries), ignore_index=True)
#             df['geometry'] = Series_geometries
#             gpdf_singlepoly = pd.concat([gpdf_singlepoly, df])
#
#         gpdf_singlepoly.reset_index(inplace=True, drop=True)
#         return gpdf_singlepoly
#
#     edges_single = multi2single(edges)
#     edges_single['geometry'] = edges_single.exterior
#     print('Generating unique edge ID...')
#     id = 1
#     for idx, row in tqdm(edges_single.iterrows(), total=edges_single.shape[0]):
#         edges_single.loc[idx, 'eID'] = id
#         id = id + 1
#         edges_single.loc[idx, 'geometry'] = Polygon(row['geometry'])
#
#     edges_clean = edges_single[['geometry', 'eID', block_id_column]]
#
#     print('Isolating islands...')
#     sindex = edges_clean.sindex
#     islands = []
#     for idx, row in edges_clean.iterrows():
#         possible_matches_index = list(sindex.intersection(row['geometry'].bounds))
#         possible_matches = edges_clean.iloc[possible_matches_index]
#         possible_matches = possible_matches.drop([idx], axis=0)
#         if possible_matches.contains(row['geometry']).any():
#             islands.append(idx)
#
#     edges_clean = edges_clean.drop(islands, axis=0)
#     print(len(islands), 'islands deleted.')
#     print('Cleaning edges...')
#     edges_clean['geometry'] = edges_clean.buffer(0.000000001)
#
#     print('Saving street edges to', save_to)
#     edges_clean.to_file(save_to)
#
#     print('Cleaning tesselation...')
#     tesselation = tesselation.drop(['street', 'mergeID'], axis=1)
#
#     print('Tesselation spatial join [1/3]...')
#     tess_centroid = tesselation.copy()
#     tess_centroid['geometry'] = tess_centroid.centroid
#
#     edg_join = edges_clean.drop(['bID'], axis=1)
#
#     print('Tesselation spatial join [2/3]...')
#     tess_with_eID = gpd.sjoin(tess_centroid, edg_join, how='left', op='intersects')
#     tess_with_eID = tess_with_eID[['uID', 'eID']]
#
#     print('Tesselation spatial join [3/3]...')
#     tesselation = tesselation.merge(tess_with_eID, on='uID')
#
#     print('Saving tesselation to', tesselation_to)
#     tesselation.to_file(tesselation_to)
#
#     print('Buildings attribute join...')
#     # attribute join cell -> building
#     tess_nid_eid = tesselation[['uID', 'eID', 'nID']]
#
#     buildings = buildings.merge(tess_nid_eid, on='uID')
#
#     print('Saving buildings to', buildings_to)
#     buildings.to_file(buildings_to)
#
#     print('Done.')
