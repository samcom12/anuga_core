
#Test of redfearns formula. Tests can be verified at
#
#http://www.cellspark.com/UTM.html
#http://www.ga.gov.au/nmd/geodesy/datums/redfearn_geo_to_grid.jsp



import unittest

from anuga.coordinate_transforms.redfearn import *
from anuga.anuga_exceptions import ANUGAError

from anuga.utilities.system_tools import get_pathname_from_package
from os.path import join
import numpy as num


#-------------------------------------------------------------

class TestCase(unittest.TestCase):

    def test_decimal_degrees_conversion(self):
        lat = degminsec2decimal_degrees(-37,39,10.15610)
        lon = degminsec2decimal_degrees(143,55,35.38390)
        assert num.allclose(lat, -37.65282114)
        assert num.allclose(lon, 143.9264955)

        dd,mm,ss = decimal_degrees2degminsec(-37.65282114)
        assert dd==-37
        assert mm==39
        assert num.allclose(ss, 10.15610)

        dd,mm,ss = decimal_degrees2degminsec(143.9264955)
        assert dd==143
        assert mm==55
        assert num.allclose(ss, 35.38390)


    def test_UTM_1(self):
        #latitude:  -37 39' 10.15610"
        #Longitude: 143 55' 35.38390"
        #Site Name:    GDA-MGA: (UTM with GRS80 ellipsoid)
        #Zone:   54
        #Easting:  758173.797  Northing: 5828674.340
        #Latitude:   -37  39 ' 10.15610 ''  Longitude: 143  55 ' 35.38390 ''
        #Grid Convergence:  1  47 ' 19.36 ''  Point Scale: 1.00042107

        lat = degminsec2decimal_degrees(-37,39,10.15610)
        lon = degminsec2decimal_degrees(143,55,35.38390)
        assert num.allclose(lat, -37.65282114)
        assert num.allclose(lon, 143.9264955)


        zone, easting, northing = redfearn(lat,lon)

        assert zone == 54
        assert num.allclose(easting, 758173.797)
        assert num.allclose(northing, 5828674.340)


    def test_UTM_2(self):
        #TEST 2

        #Latitude:  -37 57 03.7203
        #Longitude: 144 25 29.5244
        #Zone:   55
        #Easting:  273741.297  Northing: 5796489.777
        #Latitude:   -37  57 ' 3.72030 ''  Longitude: 144  25 ' 29.52440 ''
        #Grid Convergence:  -1  35 ' 3.65 ''  Point Scale: 1.00023056

        lat = degminsec2decimal_degrees(-37,57,03.7203)
        lon = degminsec2decimal_degrees(144,25,29.5244)

        zone, easting, northing = redfearn(lat,lon)

        assert zone == 55
        assert num.allclose(easting, 273741.297)
        assert num.allclose(northing, 5796489.777)


    def test_UTM_3(self):
        #Test 3
        lat = degminsec2decimal_degrees(-60,0,0)
        lon = degminsec2decimal_degrees(130,0,0)

        zone, easting, northing = redfearn(lat,lon)

        assert zone == 52
        assert num.allclose(easting, 555776.267)
        assert num.allclose(northing, 3348167.264)


    def test_UTM_4(self):
        #Test 4 (Kobenhavn, Northern hemisphere)
        lat = 55.70248
        dd,mm,ss = decimal_degrees2degminsec(lat)

        lon = 12.58364
        dd,mm,ss = decimal_degrees2degminsec(lon)

        zone, easting, northing = redfearn(lat,lon)


        assert zone == 33
        assert num.allclose(easting, 348157.631)
        assert num.allclose(northing, 6175612.993)


    def test_UTM_5(self):
        #Test 5 (Wollongong)

        lat = degminsec2decimal_degrees(-34,30,0.)
        lon = degminsec2decimal_degrees(150,55,0.)

        zone, easting, northing = redfearn(lat,lon)

        assert zone == 56
        assert num.allclose(easting, 308728.009)
        assert num.allclose(northing, 6180432.601)

    def test_UTM_6_nonstandard_projection(self):
        """test_UTM_6_nonstandard_projection

        Test that projections can be forced to
        use other than native zone.

        Data is from Geraldton, WA
        """


        # First test native projection (zone 50)
        zone, easting, northing = redfearn(-29.233299999,114.05)

        assert zone == 50
        assert num.allclose(easting, 213251.040253)
        assert num.allclose(northing, 6762559.15978)

        #Testing using the native zone
        zone, easting, northing = redfearn(-29.233299999,114.05, zone=50)

        assert zone == 50
        assert num.allclose(easting, 213251.040253)
        assert num.allclose(northing, 6762559.15978)

        #Then project to zone 49
        zone, easting, northing = redfearn(-29.233299999,114.05,zone=49)

        assert zone == 49
        assert num.allclose(easting, 796474.020057)
        assert num.allclose(northing, 6762310.25162)





        #First test native projection (zone 49)
        zone, easting, northing = redfearn(-29.1333,113.9667)

        assert zone == 49
        assert num.allclose(easting, 788653.192779)
        assert num.allclose(northing, 6773605.46384)

        #Then project to zone 50
        zone, easting, northing = redfearn(-29.1333,113.9667,zone=50)

        assert zone == 50
        assert num.allclose(easting, 204863.606467)
        assert num.allclose(northing, 6773440.04726)

        #Testing point on zone boundary
        #First test native projection (zone 50)
        zone, easting, northing = redfearn(-29.1667,114)

        assert zone == 50
        assert num.allclose(easting, 208199.768268)
        assert num.allclose(northing, 6769820.01453)

        #Then project to zone 49
        zone, easting, northing = redfearn(-29.1667,114,zone=49)

        assert zone == 49
        assert num.allclose(easting, 791800.231817)
        assert num.allclose(northing, 6769820.01453)

        #Testing furthest point in Geraldton scenario)
        #First test native projection (zone 49)
        zone, easting, northing = redfearn(-28.2167,113.4167)

        assert zone == 49
        assert num.allclose(easting, 737178.16131)
        assert num.allclose(northing, 6876426.38578)

        #Then project to zone 50
        zone, easting, northing = redfearn(-28.2167,113.4167,zone=50)

        assert zone == 50
        assert num.allclose(easting, 148260.567427)
        assert num.allclose(northing, 6873587.50926)

        #Testing outside GDA zone (New Zeland)
        #First test native projection (zone 60)
        zone, easting, northing = redfearn(-44,178)

        assert zone == 60
        assert num.allclose(easting, 580174.259843)
        assert num.allclose(northing, 5127641.114461)

        #Then project to zone 59
        zone, easting, northing = redfearn(-44,178,zone=59)

        assert zone == 59
        assert num.allclose(easting, 1061266.922118)
        assert num.allclose(northing, 5104249.395469)

        #Then skip three zones 57 (native 60)
        zone, easting, northing = redfearn(-44,178,zone=57)

        assert zone == 57
        assert num.allclose(easting, 2023865.527497)
        assert num.allclose(northing, 4949253.934967)

        # Note Projecting into the Northern Hemisphere Does not coincide
        # redfearn or ArcMap conversions. The difference lies in
        # False Northing which is 0 for the Northern hemisphere
        # in the redfearn implementation but 10 million in Arc.
        #
        # But the redfearn implementation does coincide with
        # Google Earth (he he)

        #Testing outside GDA zone (Northern Hemisphere)
        #First test native projection (zone 57)
        zone, easting, northing = redfearn(44,156)

        # Google Earth interpretation
        assert zone == 57
        assert num.allclose(easting, 259473.69)
        assert num.allclose(northing, 4876249.13)

        # ArcMap's interpretation
        #assert zone == 57
        #assert num.allclose(easting, 259473.678944)
        #assert num.allclose(northing, 14876249.1268)

        #Then project to zone 56
        zone, easting, northing = redfearn(44,156,zone=56)

        assert zone == 56
        assert num.allclose(easting, 740526.321055)
        assert num.allclose(northing, 4876249.13)




    #def test_UTM_6(self):
    #    """Test 6 (Don's Wollongong file's ref point)
    #    """
    #
    #    lat = -34.490286785873
    #    lon = 150.79712139578
    #
    #    dd,mm,ss = decimal_degrees2degminsec(lat)
    #    print dd,mm,ss
    #    dd,mm,ss = decimal_degrees2degminsec(lon)
    #    print dd,mm,ss
    #
    #    zone, easting, northing = redfearn(lat,lon)
    #
    #    print zone, easting, northing
    #
    #    assert zone == 56
    #    #assert allclose(easting, 297717.36468927) #out by 10m
    #    #assert allclose(northing, 6181725.1724276)


    def test_nonstandard_meridian_coinciding_with_native(self):
        """test_nonstandard_meridian_coinciding_with_native

        This test will verify that redfearn can be used to project
        points using an arbitrary central meridian that happens to
        coincide with the standard meridian at the center of a UTM zone.
        This is a preliminary test before testing this functionality
        with a truly arbitrary non-standard meridian.
        """

        # The file projection_test_points_z53.csv contains 10 points
        # which straddle the boundary between UTM zones 53 and 54.
        # They have been projected to zone 53 irrespective of where they
        # belong.

        path = get_pathname_from_package('anuga.coordinate_transforms')

        for forced_zone in [53, 54]:

            datafile = join(path, 'tests', 'data', 'projection_test_points_z%d.csv' % forced_zone)
            fid = open(datafile)

            for line in fid.readlines()[1:]:
                fields = line.strip().split(',')

                lon = float(fields[1])
                lat = float(fields[2])
                x = float(fields[3])
                y = float(fields[4])

                zone, easting, northing = redfearn(lat, lon,
                                                   zone=forced_zone)

                # Check calculation
                assert zone == forced_zone
                assert num.allclose(x, easting)
                assert num.allclose(y, northing)




    def test_nonstandard_meridian(self):
        """test_nonstandard_meridian

        This test will verify that redfearn can be used to project
        points using an arbitrary central meridian.
        """

        # The file projection_test_points.csv contains 10 points
        # which straddle the boundary between UTM zones 53 and 54.
        # They have been projected using a central meridian of 137.5
        # degrees (the boundary is 138 so it is pretty much right
        # in the middle of zones 53 and 54).

        path = get_pathname_from_package('anuga.coordinate_transforms')
        datafile = join(path, 'tests', 'data', 'projection_test_points.csv')
        fid = open(datafile)

        for line in fid.readlines()[1:]:
            fields = line.strip().split(',')

            lon = float(fields[1])
            lat = float(fields[2])
            x = float(fields[3])
            y = float(fields[4])

            zone, easting, northing = redfearn(lat, lon,
                                               central_meridian=137.5,
                                               scale_factor=0.9996)

            assert zone == -1 # Indicates non UTM projection
            assert num.allclose(x, easting)
            assert num.allclose(y, northing)

        # Test that zone and meridian can't both be specified
        try:
            zone, easting, northing = redfearn(lat, lon,
                                               zone=50,
                                               central_meridian=137.5)
        except Exception:
            pass
        else:
            msg = 'Should have raised exception'
            raise Exception(msg)


    def test_convert_lats_longs(self):

        #Site Name:    GDA-MGA: (UTM with GRS80 ellipsoid)
        #Zone:   56
        #Easting:  222908.705  Northing: 6233785.284
        #Latitude:   -34  0 ' 0.00000 ''  Longitude: 150  0 ' 0.00000 ''
        #Grid Convergence:  -1  40 ' 43.13 ''  Point Scale: 1.00054660

        lat_gong = degminsec2decimal_degrees(-34,30,0.)
        lon_gong = degminsec2decimal_degrees(150,55,0.)

        lat_2 = degminsec2decimal_degrees(-34,00,0.)
        lon_2 = degminsec2decimal_degrees(150,00,0.)

        lats = [lat_gong, lat_2]
        longs = [lon_gong, lon_2]
        points, zone = convert_from_latlon_to_utm(latitudes=lats, longitudes=longs)

        assert num.allclose(points[0][0], 308728.009)
        assert num.allclose(points[0][1], 6180432.601)
        assert num.allclose(points[1][0],  222908.705)
        assert num.allclose(points[1][1], 6233785.284)
        self.assertTrue(zone == 56,
                        'Bad zone error!')

    def test_convert_lats_longs2(self):

        #Site Name:    GDA-MGA: (UTM with GRS80 ellipsoid)
        #Zone:   56
        #Easting:  222908.705  Northing: 6233785.284
        #Latitude:   -34  0 ' 0.00000 ''  Longitude: 150  0 ' 0.00000 ''
        #Grid Convergence:  -1  40 ' 43.13 ''  Point Scale: 1.00054660

        lat_gong = degminsec2decimal_degrees(-34,30,0.)
        lon_gong = degminsec2decimal_degrees(150,55,0.)

        lat_2 = degminsec2decimal_degrees(34,00,0.)
        lon_2 = degminsec2decimal_degrees(100,00,0.)

        lats = [lat_gong, lat_2]
        longs = [lon_gong, lon_2]

        try:
            points, zone = convert_from_latlon_to_utm(latitudes=lats, longitudes=longs)
        except ANUGAError:
            pass
        else:
            self.assertTrue(False,
                            'Error not thrown error!')

    def test_convert_lats_longs3(self):

        #Site Name:    GDA-MGA: (UTM with GRS80 ellipsoid)
        #Zone:   56
        #Easting:  222908.705  Northing: 6233785.284
        #Latitude:   -34  0 ' 0.00000 ''  Longitude: 150  0 ' 0.00000 ''
        #Grid Convergence:  -1  40 ' 43.13 ''  Point Scale: 1.00054660

        lat_gong = "-34.5"
        lon_gong = "150.916666667"
        lat_2 = degminsec2decimal_degrees(34,00,0.)
        lon_2 = degminsec2decimal_degrees(100,00,0.)

        lats = [lat_gong, lat_2]
        longs = [lon_gong, lon_2]
        try:
            points, zone  = convert_from_latlon_to_utm(latitudes=lats, longitudes=longs)
        except ANUGAError:
            pass
        else:
            self.assertTrue(False,
                            'Error not thrown error!')

    def test_convert_latlon_to_UTM1(self):

        #Site Name:    GDA-MGA: (UTM with GRS80 ellipsoid)
        #Zone:   56
        #Easting:  222908.705  Northing: 6233785.284
        #Latitude:   -34  0 ' 0.00000 ''  Longitude: 150  0 ' 0.00000 ''
        #Grid Convergence:  -1  40 ' 43.13 ''  Point Scale: 1.00054660

        lat_gong = degminsec2decimal_degrees(-34,30,0.)
        lon_gong = degminsec2decimal_degrees(150,55,0.)

        lat_2 = degminsec2decimal_degrees(-34,00,0.)
        lon_2 = degminsec2decimal_degrees(150,00,0.)

        points = [[lat_gong, lon_gong], [lat_2, lon_2]]
        points, zone = convert_from_latlon_to_utm(points=points)
        assert num.allclose(points[0][0], 308728.009)
        assert num.allclose(points[0][1], 6180432.601)
        assert num.allclose(points[1][0],  222908.705)
        assert num.allclose(points[1][1], 6233785.284)
        self.assertTrue(zone == 56,
                        'Bad zone error!')

    def test_convert_latlon_to_UTM2(self):

        #Site Name:    GDA-MGA: (UTM with GRS80 ellipsoid)
        #Zone:   56
        #Easting:  222908.705  Northing: 6233785.284
        #Latitude:   -34  0 ' 0.00000 ''  Longitude: 150  0 ' 0.00000 ''
        #Grid Convergence:  -1  40 ' 43.13 ''  Point Scale: 1.00054660

        lat_gong = degminsec2decimal_degrees(-34,30,0.)
        lon_gong = degminsec2decimal_degrees(150,55,0.)

        lat_2 = degminsec2decimal_degrees(34,00,0.)
        lon_2 = degminsec2decimal_degrees(100,00,0.)

        points = [[lat_gong, lon_gong], [lat_2, lon_2]]

        try:
            points, zone = convert_from_latlon_to_utm(points=points)
        except ANUGAError:
            pass
        else:
            self.fail('Error not thrown error!')

    def test_convert_latlon_to_UTM3(self):

        #Site Name:    GDA-MGA: (UTM with GRS80 ellipsoid)
        #Zone:   56
        #Easting:  222908.705  Northing: 6233785.284
        #Latitude:   -34  0 ' 0.00000 ''  Longitude: 150  0 ' 0.00000 ''
        #Grid Convergence:  -1  40 ' 43.13 ''  Point Scale: 1.00054660

        lat_gong = "-34.5"
        lon_gong = "150.916666667"
        lat_2 = degminsec2decimal_degrees(34,00,0.)
        lon_2 = degminsec2decimal_degrees(100,00,0.)

        points = [[lat_gong, lon_gong], [lat_2, lon_2]]

        try:
            points, zone = convert_from_latlon_to_utm(points=points)
        except ANUGAError:
            pass
        else:
            self.fail('Error not thrown error!')

    def test_convert_latlon_to_UTM4(self):

        # Test single point

        lat_gong = -34.5
        lon_gong = 150.916666667

        points = [lat_gong, lon_gong]

        points, zone = convert_from_latlon_to_utm(points=points)

        points_ex = [[308728.0089035692, 6180432.600982175]]

        assert zone == 56
        assert num.allclose(points_ex, points)

    def test_convert_latlon_to_UTM5(self):

        # Test scalar lat and long

        lat_gong = -34.5
        lon_gong = 150.916666667

        points = [lat_gong, lon_gong]

        points, zone = convert_from_latlon_to_utm(latitudes=lat_gong, longitudes=lon_gong)

        points_ex = [[308728.0089035692, 6180432.600982175]]

        assert zone == 56
        assert num.allclose(points_ex, points)



#-------------------------------------------------------------


class Test_epsg_conversions(unittest.TestCase):
    """Tests for epsg_to_ll and ll_to_epsg added in this PR.

    These functions wrap pyproj.Transformer and were newly exported from
    anuga.coordinate_transforms.redfearn (and re-exported from anuga.__init__).
    """

    def _skip_if_no_pyproj(self):
        try:
            import pyproj  # noqa: F401
        except ImportError:
            self.skipTest('pyproj not available')

    # ------------------------------------------------------------------
    # epsg_to_ll
    # ------------------------------------------------------------------

    def test_epsg_to_ll_scalar_utm_zone55s(self):
        """Single UTM easting/northing → lat/lon (EPSG:32755 = UTM zone 55S)."""
        self._skip_if_no_pyproj()
        from anuga.coordinate_transforms.redfearn import epsg_to_ll
        # Known point: Sydney Opera House ≈ (151.215E, -33.857S)
        easting = 334111.0
        northing = 6251099.0
        epsg = 32755  # WGS 84 / UTM zone 55S
        lat, lon = epsg_to_ll(easting, northing, epsg)
        self.assertAlmostEqual(float(lat), -33.857, delta=0.01)
        self.assertAlmostEqual(float(lon), 151.215, delta=0.01)

    def test_epsg_to_ll_array_utm_zone55s(self):
        """Array of UTM coordinates → lat/lon arrays."""
        self._skip_if_no_pyproj()
        from anuga.coordinate_transforms.redfearn import epsg_to_ll
        easting = num.array([334111.0, 334111.0])
        northing = num.array([6251099.0, 6251099.0])
        epsg = 32755
        lat, lon = epsg_to_ll(easting, northing, epsg)
        self.assertEqual(lat.shape, (2,))
        self.assertEqual(lon.shape, (2,))
        num.testing.assert_allclose(lat, lat[0])  # both same point
        num.testing.assert_allclose(lon, lon[0])

    def test_epsg_to_ll_returns_ndarrays(self):
        """epsg_to_ll always returns numpy arrays regardless of scalar input."""
        self._skip_if_no_pyproj()
        from anuga.coordinate_transforms.redfearn import epsg_to_ll
        lat, lon = epsg_to_ll(334111.0, 6251099.0, 32755)
        self.assertIsInstance(lat, num.ndarray)
        self.assertIsInstance(lon, num.ndarray)

    # ------------------------------------------------------------------
    # ll_to_epsg
    # ------------------------------------------------------------------

    def test_ll_to_epsg_scalar_utm_zone55s(self):
        """Single lat/lon → UTM easting/northing (EPSG:32755)."""
        self._skip_if_no_pyproj()
        from anuga.coordinate_transforms.redfearn import ll_to_epsg
        lat = -33.857
        lon = 151.215
        epsg = 32755
        easting, northing = ll_to_epsg(lat, lon, epsg)
        self.assertAlmostEqual(float(easting), 334111.0, delta=10.0)
        self.assertAlmostEqual(float(northing), 6251099.0, delta=10.0)

    def test_ll_to_epsg_array_utm_zone55s(self):
        """Array of lat/lon coordinates → UTM easting/northing arrays."""
        self._skip_if_no_pyproj()
        from anuga.coordinate_transforms.redfearn import ll_to_epsg
        lat = num.array([-33.857, -33.857])
        lon = num.array([151.215, 151.215])
        epsg = 32755
        easting, northing = ll_to_epsg(lat, lon, epsg)
        self.assertEqual(easting.shape, (2,))
        self.assertEqual(northing.shape, (2,))

    def test_ll_to_epsg_returns_ndarrays(self):
        """ll_to_epsg always returns numpy arrays."""
        self._skip_if_no_pyproj()
        from anuga.coordinate_transforms.redfearn import ll_to_epsg
        easting, northing = ll_to_epsg(-33.857, 151.215, 32755)
        self.assertIsInstance(easting, num.ndarray)
        self.assertIsInstance(northing, num.ndarray)

    # ------------------------------------------------------------------
    # Round-trip
    # ------------------------------------------------------------------

    def test_epsg_ll_roundtrip_scalar(self):
        """ll_to_epsg followed by epsg_to_ll recovers original coordinates."""
        self._skip_if_no_pyproj()
        from anuga.coordinate_transforms.redfearn import epsg_to_ll, ll_to_epsg
        lat0, lon0 = -33.857, 151.215
        epsg = 32755
        e, n = ll_to_epsg(lat0, lon0, epsg)
        lat1, lon1 = epsg_to_ll(e, n, epsg)
        self.assertAlmostEqual(float(lat1), lat0, delta=1e-5)
        self.assertAlmostEqual(float(lon1), lon0, delta=1e-5)

    def test_epsg_ll_roundtrip_array(self):
        """Array round-trip for ll_to_epsg / epsg_to_ll."""
        self._skip_if_no_pyproj()
        from anuga.coordinate_transforms.redfearn import epsg_to_ll, ll_to_epsg
        lat0 = num.array([-33.857, -34.5, -35.0])
        lon0 = num.array([151.215, 150.9, 149.5])
        epsg = 32755
        e, n = ll_to_epsg(lat0, lon0, epsg)
        lat1, lon1 = epsg_to_ll(e, n, epsg)
        num.testing.assert_allclose(lat1, lat0, atol=1e-5)
        num.testing.assert_allclose(lon1, lon0, atol=1e-5)

    # ------------------------------------------------------------------
    # Exported from anuga.__init__
    # ------------------------------------------------------------------

    def test_anuga_init_exports_epsg_to_ll(self):
        """epsg_to_ll is importable directly from anuga."""
        import anuga
        self.assertTrue(hasattr(anuga, 'epsg_to_ll'))

    def test_anuga_init_exports_ll_to_epsg(self):
        """ll_to_epsg is importable directly from anuga."""
        import anuga
        self.assertTrue(hasattr(anuga, 'll_to_epsg'))

    def test_anuga_all_contains_epsg_to_ll(self):
        """epsg_to_ll appears in anuga.__all__."""
        import anuga
        self.assertIn('epsg_to_ll', anuga.__all__)

    def test_anuga_all_contains_ll_to_epsg(self):
        """ll_to_epsg appears in anuga.__all__."""
        import anuga
        self.assertIn('ll_to_epsg', anuga.__all__)


#-------------------------------------------------------------

if __name__ == "__main__":
    suite = unittest.TestLoader().loadTestsFromTestCase(TestCase)
    runner = unittest.TextTestRunner()
    runner.run(suite)
