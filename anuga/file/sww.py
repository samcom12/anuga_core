""" Classes to read an SWW file.
"""

import numpy
import numpy as num
import threading
import concurrent.futures
from anuga.utilities.file_utils import create_filename
from anuga.coordinate_transforms.geo_reference import \
    ensure_geo_reference
from .sts import Write_sts
from anuga.config import minimum_storable_height as default_minimum_storable_height
from anuga.config import institution as default_institution
from anuga.file.netcdf import NetCDFFile
import anuga.utilities.log as log
from anuga.utilities.numerical_tools import ensure_numeric
from anuga.config import max_float
from anuga.config import netcdf_float, netcdf_float32, netcdf_int, netcdf_float64
from anuga.config import netcdf_mode_r, netcdf_mode_w, netcdf_mode_a
from anuga.coordinate_transforms.geo_reference import Geo_reference



class DataFileNotOpenError(Exception):
    pass


class DataMissingValuesError(Exception):
    pass


class NewQuantity(Exception):
    pass


class DataDomainError(Exception):
    pass


class DataTimeError(Exception):
    pass


class Data_format:
    """Generic interface to data formats
    """

    def __init__(self, domain, extension, mode=netcdf_mode_w):
        assert mode[0] in ['r', 'w', 'a'], \
            "Mode %s must be either:\n" % mode + \
            "   'w' (write)\n" + \
            "   'r' (read)\n" + \
            "   'a' (append)"

        # Create filename
        self.filename = create_filename(domain.get_datadir(),
                                        domain.get_name(), extension)

        self.timestep = 0
        self.domain = domain

        # Probably should exclude ghosts in case this is a parallel domain

        self.number_of_nodes = domain.number_of_nodes
        self.number_of_volumes = domain.number_of_triangles
        #self.number_of_volumes = len(domain)

        # FIXME: Should we have a general set_precision function?


class SWW_file(Data_format):
    """Interface to native NetCDF format (.sww) for storing model output

    There are two kinds of data

    1: Constant data: Vertex coordinates and field values. Stored once
    2: Variable data: Conserved quantities. Stored once per timestep.

    All data is assumed to reside at vertex locations.
    """

    def __init__(self, domain,
                 mode=netcdf_mode_w, max_size=200000000000, recursion=False):

        self.precision = netcdf_float32  # Use single precision for quantities
        self.recursion = recursion
        self.mode = mode

        if hasattr(domain, 'max_size'):
            self.max_size = domain.max_size  # File size max is 2Gig
        else:
            self.max_size = max_size

        if hasattr(domain, 'store_centroids'):
            self.store_centroids = domain.store_centroids
        else:
            self.store_centroids = False

        if hasattr(domain, 'institution'):
            self.institution = domain.institution
        else:
            self.institution = default_institution

        if hasattr(domain, 'minimum_storable_height'):
            self.minimum_storable_height = domain.minimum_storable_height
        else:
            self.minimum_storable_height = default_minimum_storable_height

        if hasattr(domain, 'timezone'):
            self.timezone = str(domain.get_timezone())
        else:
            self.timezone = 'UTC'

        # zlib compression for dynamic variables (requires NETCDF4 format)
        self.zlib_compression = bool(getattr(domain, 'sww_zlib_compression', False))
        self.zlib_complevel = int(getattr(domain, 'sww_zlib_complevel', 4))
        self._nc_format = 'NETCDF4' if self.zlib_compression else None

        # Async write: collect data synchronously, write in background thread
        self.async_write = bool(getattr(domain, 'sww_async_write', False))
        self._write_executor = None
        self._pending_future = None
        if self.async_write:
            self._write_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)


        # Call parent constructor
        Data_format.__init__(self, domain, 'sww', mode)

        # Get static and dynamic quantities from domain
        static_quantities = []
        dynamic_quantities = []
        static_c_quantities = []
        dynamic_c_quantities = []

        for q in domain.quantities_to_be_stored:
            flag = domain.quantities_to_be_stored[q]

            msg = 'Quantity %s is requested to be stored ' % q
            msg += 'but it does not exist in domain.quantities'
            assert q in domain.quantities, msg

            assert flag in [1, 2]
            if flag == 1:
                static_quantities.append(q)
                if self.store_centroids:
                    static_c_quantities.append(q+'_c')

            if flag == 2:
                dynamic_quantities.append(q)
                if self.store_centroids:
                    dynamic_c_quantities.append(q+'_c')

        # NetCDF file definition
        fid = NetCDFFile(self.filename, mode, nc_format=self._nc_format)
        if mode[0] == 'w':
            description = 'Output from anuga.file.sww ' \
                          'suitable for plotting'

            self.writer = Write_sww(static_quantities,
                                    dynamic_quantities,
                                    static_c_quantities,
                                    dynamic_c_quantities)

            self.writer.store_header(fid,
                                     domain.starttime,
                                     self.number_of_volumes,
                                     self.domain.number_of_nodes,
                                     description=description,
                                     institution=self.institution,
                                     smoothing=domain.smooth,
                                     order=domain.default_order,
                                     sww_precision=self.precision,
                                     timezone=self.timezone,
                                     zlib=self.zlib_compression,
                                     complevel=self.zlib_complevel)

            # Extra optional information
            if hasattr(domain, 'texture'):
                fid.texture = domain.texture

            if domain.quantities_to_be_monitored is not None:
                fid.createDimension('singleton', 1)
                fid.createDimension('two', 2)

                poly = domain.monitor_polygon
                if poly is not None:
                    N = len(poly)
                    fid.createDimension('polygon_length', N)
                    fid.createVariable('extrema.polygon',
                                       self.precision,
                                       ('polygon_length', 'two'))
                    fid.variables['extrema.polygon'][:] = poly

                interval = domain.monitor_time_interval
                if interval is not None:
                    fid.createVariable('extrema.time_interval',
                                       self.precision,
                                       ('two',))
                    fid.variables['extrema.time_interval'][:] = interval

                for q in domain.quantities_to_be_monitored:
                    fid.createVariable(q + '.extrema', self.precision,
                                       ('numbers_in_range',))
                    fid.createVariable(q + '.min_location', self.precision,
                                       ('numbers_in_range',))
                    fid.createVariable(q + '.max_location', self.precision,
                                       ('numbers_in_range',))
                    fid.createVariable(q + '.min_time', self.precision,
                                       ('singleton',))
                    fid.createVariable(q + '.max_time', self.precision,
                                       ('singleton',))

        fid.close()

    def store_connectivity(self):
        """Store information about nodes, triangles and static quantities

        Writes x,y coordinates of triangles and their connectivity.

        Store also any quantity that has been identified as static.
        """

        # FIXME: Change name to reflect the fact thta this function
        # stores both connectivity (triangulation) and static quantities

        domain = self.domain

        # append to the NetCDF file
        fid = NetCDFFile(self.filename, netcdf_mode_a)

        # Get X, Y from one (any) of the quantities
        Q = list(domain.quantities.values())[0]
        X, Y, _, V = Q.get_vertex_values(xy=True, precision=self.precision)

        # store the connectivity data
        points = num.concatenate(
            (X[:, num.newaxis], Y[:, num.newaxis]), axis=1)
        self.writer.store_triangulation(fid,
                                        points,
                                        V.astype(num.float32),
                                        points_georeference=domain.geo_reference)

        if domain.parallel:
            self.writer.store_parallel_data(fid,
                                            domain.number_of_global_triangles,
                                            domain.number_of_global_nodes,
                                            domain.tri_full_flag,
                                            domain.tri_l2g,
                                            domain.node_l2g)

        # Get names of static quantities
        static_quantities = {}
        static_quantities_centroid = {}

        for name in self.writer.static_quantities:
            Q = domain.quantities[name]
            A, _ = Q.get_vertex_values(xy=False,
                                       precision=self.precision)
            static_quantities[name] = A

        # print domain.quantities
        # print self.writer.static_c_quantities

        for name in self.writer.static_c_quantities:
            Q = domain.quantities[name[:-2]]  # rip off _c from name
            static_quantities_centroid[name] = Q.centroid_values

        # Store static quantities
        self.writer.store_static_quantities(fid, **static_quantities)
        self.writer.store_static_quantities_centroid(
            fid, **static_quantities_centroid)

        fid.close()

    def store_timestep(self):
        """Store time and time dependent quantities.

        When ``async_write`` is enabled (set via ``domain.sww_async_write =
        True``), quantity data is collected synchronously from the domain and
        then written to the NetCDF file in a background thread, allowing the
        simulation to continue computing while the IO is in progress.
        """

        # Wait for any previously submitted async write to complete before
        # collecting new data.  Any exception raised by the background write
        # is re-raised here at the next timestep so it is visible to the caller.
        if self.async_write and self._pending_future is not None:
            try:
                self._pending_future.result()
            except Exception as exc:
                raise RuntimeError(
                    'Async SWW write failed for file %s: %s' % (self.filename, exc)
                ) from exc
            finally:
                self._pending_future = None

        # Collect all data needed for this timestep synchronously so that
        # the domain can safely advance before the file write completes.
        snapshot = self._collect_timestep_snapshot()
        if snapshot is None:
            # Recursion case already handled inside _collect_timestep_snapshot
            return

        if self.async_write:
            self._pending_future = self._write_executor.submit(
                self._write_timestep_snapshot, snapshot)
        else:
            self._write_timestep_snapshot(snapshot)

    def _collect_timestep_snapshot(self):
        """Collect a snapshot of all data required for the current timestep.

        Returns a dict ready to pass to ``_write_timestep_snapshot``, or
        ``None`` if the file-size recursion path was taken (which writes
        synchronously internally and needs no further action).
        """

        from time import sleep
        from os import stat

        # Open the file to check size and read the current time length.
        retries = 0
        file_open = False
        fid = None
        while not file_open and retries < 10:
            try:
                fid = NetCDFFile(self.filename, netcdf_mode_a)
            except OSError:
                msg = 'Warning (store_timestep): File %s could not be opened' \
                      % self.filename
                msg += ' - trying step %s again' % self.domain.relative_time
                log.info(msg)
                retries += 1
                sleep(1)
            else:
                file_open = True

        if not file_open:
            msg = 'File %s could not be opened for append' % self.filename
            raise DataFileNotOpenError(msg)

        # Check to see if the file is already too big:
        time_len = fid.variables['time'].shape[0]
        i = len(time_arr) + 1
        file_size = stat(self.filename)[6]
        file_size_increase = file_size // i
        fid.close()

        if file_size + file_size_increase > self.max_size * 2**self.recursion:
            # File too large — create a new SWW file synchronously (rare path).
            filename_ext = '_time_%s' % self.domain.relative_time
            filename_ext = filename_ext.replace('.', '_')
            old_domain_filename = self.domain.get_name()
            if not self.recursion:
                self.domain.set_name(old_domain_filename + filename_ext)
            old_domain_starttime = self.domain.starttime
            self.domain.starttime = self.domain.get_time()
            next_data_structure = SWW_file(self.domain, mode=self.mode,
                                           max_size=self.max_size,
                                           recursion=self.recursion + 1)
            if not self.recursion:
                log.info('    file_size = %s' % file_size)
                log.info('    saving file to %s'
                         % next_data_structure.filename)
            self.domain.writer = next_data_structure
            next_data_structure.store_connectivity()
            next_data_structure.store_timestep()
            # Restore
            self.domain.starttime = old_domain_starttime
            self.domain.set_name(old_domain_filename)
            return None  # Handled; caller should not write further.

        # Normal path: collect all data from the domain now (synchronously).
        self.recursion = False
        domain = self.domain
        relative_time = domain.relative_time

        z = None
        storable_indices = None
        if 'stage' in self.writer.dynamic_quantities:
            Q = domain.quantities['stage']
            w, _ = Q.get_vertex_values(xy=False)
            Q = domain.quantities['elevation']
            z, _ = Q.get_vertex_values(xy=False)
            storable_indices = num.array(w - z >= self.minimum_storable_height)

        dynamic_quantities = {}
        for name in self.writer.dynamic_quantities:
            Q = domain.quantities[name]
            # get_vertex_values returns a newly allocated array, so no explicit
            # copy is needed here for thread safety.
            A, _ = Q.get_vertex_values(xy=False, precision=self.precision)
            if storable_indices is not None:
                if name == 'stage':
                    A = num.choose(storable_indices, (z, A))
                if name in ['xmomentum', 'ymomentum']:
                    null = num.zeros(num.size(A), A.dtype.char)
                    A = num.choose(storable_indices, (null, A))
            dynamic_quantities[name] = A

        dynamic_quantities_centroid = {}
        for name in self.writer.dynamic_c_quantities:
            Q = domain.quantities[name[:-2]]
            # centroid_values is a live array — take a copy so the background
            # thread sees the values at *this* timestep even after the domain
            # advances.
            dynamic_quantities_centroid[name] = Q.centroid_values.copy()

        # Snapshot the extrema (monitored quantities) if needed.
        extrema_snapshot = None
        if domain.quantities_to_be_monitored is not None:
            extrema_snapshot = {}
            for q, info in domain.quantities_to_be_monitored.items():
                extrema_snapshot[q] = {
                    'min': info['min'],
                    'min_location': (info['min_location'].copy()
                                     if info['min_location'] is not None else None),
                    'min_time': info['min_time'],
                    'max': info['max'],
                    'max_location': (info['max_location'].copy()
                                     if info['max_location'] is not None else None),
                    'max_time': info['max_time'],
                }

        return {
            'filename': self.filename,
            'relative_time': relative_time,
            'precision': self.precision,
            'dynamic_quantities': dynamic_quantities,
            'dynamic_quantities_centroid': dynamic_quantities_centroid,
            'store_centroids': self.store_centroids,
            'extrema_snapshot': extrema_snapshot,
            'writer': self.writer,
        }

    def _write_timestep_snapshot(self, snapshot):
        """Write a previously collected snapshot to the NetCDF file.

        This method is safe to call from a background thread because it only
        uses data already captured in *snapshot* — it does not touch the domain.
        """

        from time import sleep

        filename = snapshot['filename']
        retries = 0
        file_open = False
        fid = None
        while not file_open and retries < 10:
            try:
                fid = NetCDFFile(filename, netcdf_mode_a)
            except OSError:
                msg = 'Warning (_write_timestep_snapshot): File %s could not be opened' \
                      % filename
                log.info(msg)
                retries += 1
                sleep(1)
            else:
                file_open = True

        if not file_open:
            raise DataFileNotOpenError(
                'File %s could not be opened for append' % filename)

        writer = snapshot['writer']
        slice_index = writer.store_quantities(
            fid,
            time=snapshot['relative_time'],
            sww_precision=snapshot['precision'],
            **snapshot['dynamic_quantities'])

        if snapshot['store_centroids']:
            writer.store_quantities_centroid(
                fid,
                slice_index=slice_index,
                sww_precision=snapshot['precision'],
                **snapshot['dynamic_quantities_centroid'])

        if snapshot['extrema_snapshot'] is not None:
            for q, info in snapshot['extrema_snapshot'].items():
                if info['min'] is not None:
                    fid.variables[q + '.extrema'][0] = info['min']
                    fid.variables[q + '.min_location'][:] = info['min_location']
                    fid.variables[q + '.min_time'][0] = info['min_time']
                if info['max'] is not None:
                    fid.variables[q + '.extrema'][1] = info['max']
                    fid.variables[q + '.max_location'][:] = info['max_location']
                    fid.variables[q + '.max_time'][0] = info['max_time']

        fid.close()

    def wait_for_pending_write(self):
        """Block until any background write submitted by the async path completes.

        Raises RuntimeError if the background write encountered an exception,
        so the caller is notified of any IO failure.
        """

        if self._pending_future is not None:
            try:
                self._pending_future.result()
            except Exception as exc:
                raise RuntimeError(
                    'Async SWW write failed for file %s: %s' % (self.filename, exc)
                ) from exc
            finally:
                self._pending_future = None

    def close(self):
        """Flush any pending async write and shut down the background executor.

        Call this explicitly after the last ``store_timestep`` to guarantee
        all data is written to disk before continuing.  This is also called
        automatically from ``__del__``, but explicit calls are preferred.
        """

        self.wait_for_pending_write()
        if self._write_executor is not None:
            self._write_executor.shutdown(wait=True)
            self._write_executor = None

    def __del__(self):
        """Ensure any pending async write finishes before the object is garbage-collected."""

        try:
            self.close()
        except Exception as exc:
            log.info('SWW_file.__del__: error during async write cleanup: %s' % exc)


class Parallel_SWW_file:
    """Parallel NetCDF MPI-IO writer for SWW files.

    All MPI ranks write collectively to a single shared .sww file using
    netCDF4 parallel I/O (HDF5 backend).  This eliminates the post-run
    ``sww_merge`` step and improves write throughput on parallel filesystems.

    Requires
    --------
    * ``netCDF4`` built with parallel HDF5 support
      (``nc-config --has-parallel4`` should print ``yes``).
    * ``mpi4py`` installed.
    * The domain must have ``tri_l2g``, ``node_l2g``, ``tri_full_flag``,
      ``number_of_global_triangles``, and ``number_of_global_nodes``
      attributes (all present on ``Parallel_domain`` instances).

    Usage
    -----
    Set ``domain.sww_parallel_write = True`` before the evolve loop::

        domain.sww_parallel_write = True
        for t in domain.evolve(yieldstep=10, finaltime=100):
            ...
        # No sww_merge() call needed — the single shared file is complete.

    Only smooth-mesh mode (``domain.smooth = True``, the default for
    parallel domains) is supported.
    """

    def __init__(self, domain, mode=netcdf_mode_w, max_size=None,
                 recursion=False):
        try:
            from mpi4py import MPI
            from netCDF4 import Dataset  # noqa: F401 — verify import works
        except ImportError as exc:
            raise ImportError(
                "Parallel_SWW_file requires mpi4py and netCDF4 with "
                "parallel HDF5 support.  Error: %s" % exc
            ) from exc

        from mpi4py import MPI

        self._MPI = MPI
        self._comm = MPI.COMM_WORLD
        self._rank = domain.processor
        self._nprocs = domain.numproc

        # Local-to-global index maps (set by Parallel_domain constructor)
        self._tri_l2g = domain.tri_l2g
        self._node_l2g = domain.node_l2g
        self._tri_full_flag = domain.tri_full_flag
        self._n_global_triangles = domain.number_of_global_triangles
        self._n_global_nodes = domain.number_of_global_nodes

        self.precision = netcdf_float32
        self.domain = domain
        self._timestep_count = 0

        # Compression (requires NETCDF4 format — already enforced here)
        self.zlib_compression = bool(getattr(domain, 'sww_zlib_compression', False))
        self.zlib_complevel = int(getattr(domain, 'sww_zlib_complevel', 4))

        # Centroid storage
        self.store_centroids = bool(getattr(domain, 'store_centroids', False))

        # Minimum storable height
        if hasattr(domain, 'minimum_storable_height'):
            self.minimum_storable_height = domain.minimum_storable_height
        else:
            self.minimum_storable_height = default_minimum_storable_height

        # Shared filename: uses the global (non-rank-specific) name
        self.filename = create_filename(
            domain.get_datadir(), domain.get_global_name(), 'sww')

        # Classify stored quantities
        static_quantities = []
        dynamic_quantities = []
        static_c_quantities = []
        dynamic_c_quantities = []

        if not domain.smooth:
            raise ValueError(
                "Parallel_SWW_file only supports smooth-mesh mode "
                "(domain.smooth=True).  Non-smooth (unique-vertex) mode "
                "is not yet implemented for parallel MPI-IO.")

        for q in domain.quantities_to_be_stored:
            flag = domain.quantities_to_be_stored[q]
            assert q in domain.quantities, \
                'Quantity %s not in domain.quantities' % q
            assert flag in [1, 2]
            if flag == 1:
                static_quantities.append(q)
                if self.store_centroids:
                    static_c_quantities.append(q + '_c')
            if flag == 2:
                dynamic_quantities.append(q)
                if self.store_centroids:
                    dynamic_c_quantities.append(q + '_c')

        self.static_quantities = static_quantities
        self.dynamic_quantities = dynamic_quantities
        self.static_c_quantities = static_c_quantities
        self.dynamic_c_quantities = dynamic_c_quantities

        # Create the file (all ranks participate collectively)
        if mode[0] == 'w':
            self._create_file_header(domain)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _open(self, mode='r+'):
        """Open the shared NetCDF file in parallel mode (collective)."""
        from mpi4py import MPI
        from netCDF4 import Dataset

        try:
            fid = Dataset(
                self.filename, mode, format='NETCDF4',
                parallel=True, comm=self._comm, info=MPI.Info())
        except Exception as exc:
            raise RuntimeError(
                "Failed to open '%s' in parallel netCDF4 mode.  "
                "Ensure netCDF4 was compiled with parallel HDF5 support "
                "(nc-config --has-parallel4 should print 'yes').  "
                "Error: %s" % (self.filename, exc)
            ) from exc
        return fid

    def _full_nodes_of_rank(self, V):
        """Return (fl_nodes, g_full_nodes) for this rank's full triangles.

        Parameters
        ----------
        V : ndarray, shape (n_local_tris, 3)
            Local vertex-index connectivity array.

        Returns
        -------
        fl_nodes : ndarray of int
            Unique local node indices that belong to at least one full tri.
        g_full_nodes : ndarray of int
            Corresponding global node indices (``node_l2g[fl_nodes]``).
        """
        full_mask = self._tri_full_flag > 0
        V_full = V[full_mask].astype(int)
        if V_full.size > 0:
            fl_nodes = num.unique(V_full)
            g_full_nodes = self._node_l2g[fl_nodes]
        else:
            fl_nodes = num.array([], dtype=int)
            g_full_nodes = num.array([], dtype=int)
        return fl_nodes, g_full_nodes

    # ------------------------------------------------------------------
    # File creation
    # ------------------------------------------------------------------

    def _create_file_header(self, domain):
        """Create the NETCDF4 file with global dimensions (collective)."""
        fid = self._open('w')

        # --- Global metadata ---
        fid.institution = getattr(domain, 'institution', default_institution)
        fid.description = 'Output from anuga.file.sww suitable for plotting'

        if domain.smooth:
            fid.smoothing = 'Yes'
            fid.vertices_are_stored_uniquely = 'False'
        else:
            fid.smoothing = 'No'
            fid.vertices_are_stored_uniquely = 'True'

        fid.order = domain.default_order
        fid.starttime = float(domain.starttime)
        fid.timezone = str(getattr(domain, 'timezone', 'UTC'))

        from anuga import get_revision_number, get_revision_date, get_version
        try:
            fid.revision_number = str(get_revision_number())
        except Exception:
            fid.revision_number = 'None'
        try:
            fid.revision_date = str(get_revision_date())
        except Exception:
            fid.revision_date = 'None'
        try:
            fid.anuga_version = str(get_version())
        except Exception:
            fid.anuga_version = 'None'

        # Geo-reference attributes (xllcorner, yllcorner, zone, …)
        domain.geo_reference.write_NetCDF(fid)

        # --- Dimensions ---
        fid.createDimension('number_of_volumes', self._n_global_triangles)
        fid.createDimension('number_of_triangle_vertices',
                            self._n_global_nodes)
        fid.createDimension('number_of_vertices', 3)
        fid.createDimension('numbers_in_range', 2)
        fid.createDimension('number_of_points', self._n_global_nodes)
        fid.createDimension('number_of_timesteps', None)   # unlimited

        # --- Static geometry variables ---
        fid.createVariable('x', self.precision, ('number_of_points',))
        fid.createVariable('y', self.precision, ('number_of_points',))
        fid.createVariable('volumes', netcdf_int,
                           ('number_of_volumes', 'number_of_vertices'))

        for q in self.static_quantities:
            fid.createVariable(q, self.precision, ('number_of_points',))
            v = fid.createVariable(q + '_range', self.precision,
                                   ('numbers_in_range',))
            v[0] = max_float
            v[1] = -max_float

        for q in self.static_c_quantities:
            fid.createVariable(q, self.precision, ('number_of_volumes',))

        # --- Dynamic variables ---
        compress_kwargs = {}
        if self.zlib_compression:
            compress_kwargs = {'zlib': True, 'complevel': self.zlib_complevel}

        for q in self.dynamic_quantities:
            var = fid.createVariable(
                q, self.precision,
                ('number_of_timesteps', 'number_of_points'),
                **compress_kwargs)
            var.set_collective(True)   # collective I/O for unlimited dim
            v = fid.createVariable(q + '_range', self.precision,
                                   ('numbers_in_range',))
            v[0] = max_float
            v[1] = -max_float

        for q in self.dynamic_c_quantities:
            var = fid.createVariable(
                q, self.precision,
                ('number_of_timesteps', 'number_of_volumes'),
                **compress_kwargs)
            var.set_collective(True)

        time_var = fid.createVariable(
            'time', netcdf_float, ('number_of_timesteps',))
        time_var.set_collective(True)

        fid.close()

    # ------------------------------------------------------------------
    # Public interface (mirrors SWW_file)
    # ------------------------------------------------------------------

    def store_connectivity(self):
        """Write mesh geometry and static quantities in parallel.

        All ranks participate collectively.  Each rank writes its own
        local nodes/triangles to the correct global positions.  Ghost
        triangles are excluded; ghost nodes are written by their owning rank.
        """
        domain = self.domain
        node_l2g = self._node_l2g
        tri_l2g = self._tri_l2g
        full_mask = self._tri_full_flag > 0

        # Vertex coordinates and connectivity
        Q_ref = list(domain.quantities.values())[0]
        X, Y, _, V = Q_ref.get_vertex_values(xy=True, precision=self.precision)
        # X, Y: shape (n_local_nodes,)
        # V:    shape (n_local_tris, 3) — local node indices

        fl_nodes, g_full_nodes = self._full_nodes_of_rank(V)

        fid = self._open('r+')

        # Enable collective I/O on static geometry (helps HDF5 performance)
        fid.variables['x'].set_collective(True)
        fid.variables['y'].set_collective(True)
        fid.variables['volumes'].set_collective(True)

        # x, y: write all local nodes to their global positions.
        # Nodes on partition boundaries are shared between ranks and will be
        # written by each rank that owns them.  Since coordinates are
        # identical across ranks for shared nodes, concurrent writes are
        # safe.  A future optimisation could assign exclusive ownership
        # (e.g. each node written only by the lowest-rank owner) to
        # reduce write contention on large parallel filesystems.
        fid.variables['x'][node_l2g] = X
        fid.variables['y'][node_l2g] = Y

        # volumes: full triangles only, translating to global node indices
        V_full = V[full_mask].astype(int)
        tri_l2g_full = tri_l2g[full_mask]
        if V_full.size > 0:
            g_volumes = node_l2g[V_full]   # (n_full_tris, 3) global indices
            fid.variables['volumes'][tri_l2g_full] = g_volumes

        # Static vertex quantities (full nodes only)
        for name in self.static_quantities:
            Q = domain.quantities[name]
            A, _ = Q.get_vertex_values(xy=False, precision=self.precision)
            fid.variables[name].set_collective(True)
            if g_full_nodes.size > 0:
                fid.variables[name][g_full_nodes] = A[fl_nodes]
                local_min = float(num.min(A[fl_nodes]))
                local_max = float(num.max(A[fl_nodes]))
            else:
                local_min = max_float
                local_max = -max_float
            # All ranks participate in Allreduce (required for MPI correctness)
            g_min = self._comm.allreduce(local_min, op=self._MPI.MIN)
            g_max = self._comm.allreduce(local_max, op=self._MPI.MAX)
            if self._rank == 0:
                fid.variables[name + '_range'][0] = g_min
                fid.variables[name + '_range'][1] = g_max

        # Static centroid quantities (full triangles only)
        for name in self.static_c_quantities:
            q_name = name[:-2]
            Q = domain.quantities[q_name]
            fid.variables[name].set_collective(True)
            if full_mask.any():
                fid.variables[name][tri_l2g[full_mask]] = \
                    Q.centroid_values[full_mask].astype(self.precision)

        fid.close()

    def store_timestep(self):
        """Write dynamic quantities for the current timestep in parallel.

        All ranks participate collectively.  Each rank writes to the global
        node/triangle indices corresponding to its full (non-ghost) cells.
        """
        domain = self.domain
        node_l2g = self._node_l2g
        tri_l2g = self._tri_l2g
        full_mask = self._tri_full_flag > 0
        t_idx = self._timestep_count

        Q_ref = list(domain.quantities.values())[0]
        _, _, _, V = Q_ref.get_vertex_values(xy=True, precision=self.precision)
        fl_nodes, g_full_nodes = self._full_nodes_of_rank(V)

        fid = self._open('r+')

        # Time variable (collective — extends unlimited dimension)
        fid.variables['time'].set_collective(True)
        fid.variables['time'][t_idx] = domain.relative_time

        # Storable-height mask (wet/dry)
        z = None
        storable_indices = None
        if 'stage' in self.dynamic_quantities:
            w, _ = domain.quantities['stage'].get_vertex_values(xy=False)
            z, _ = domain.quantities['elevation'].get_vertex_values(xy=False)
            storable_indices = num.array(
                w - z >= self.minimum_storable_height)

        # Dynamic vertex quantities
        for name in self.dynamic_quantities:
            Q = domain.quantities[name]
            A, _ = Q.get_vertex_values(xy=False, precision=self.precision)
            if storable_indices is not None:
                if name == 'stage':
                    A = num.choose(storable_indices, (z, A))
                elif name in ['xmomentum', 'ymomentum']:
                    zero_momentum = num.zeros(num.size(A), A.dtype.char)
                    A = num.choose(storable_indices, (zero_momentum, A))

            fid.variables[name].set_collective(True)
            if g_full_nodes.size > 0:
                fid.variables[name][t_idx, g_full_nodes] = A[fl_nodes]
                local_min = float(num.min(A[fl_nodes]))
                local_max = float(num.max(A[fl_nodes]))
            else:
                local_min = max_float
                local_max = -max_float
            # All ranks participate in Allreduce (required for MPI correctness)
            g_min = self._comm.allreduce(local_min, op=self._MPI.MIN)
            g_max = self._comm.allreduce(local_max, op=self._MPI.MAX)
            if self._rank == 0:
                q_range = fid.variables[name + '_range'][:]
                if g_min < q_range[0]:
                    fid.variables[name + '_range'][0] = g_min
                if g_max > q_range[1]:
                    fid.variables[name + '_range'][1] = g_max

        # Dynamic centroid quantities
        for name in self.dynamic_c_quantities:
            q_name = name[:-2]
            Q = domain.quantities[q_name]
            fid.variables[name].set_collective(True)
            if full_mask.any():
                fid.variables[name][t_idx, tri_l2g[full_mask]] = \
                    Q.centroid_values[full_mask].astype(self.precision)

        fid.close()
        self._timestep_count += 1

    def wait_for_pending_write(self):
        """No async writes in parallel mode — always synchronous."""
        pass

    def close(self):
        """No background executor to shut down in parallel mode."""
        pass

    def __del__(self):
        pass


class Read_sww:

    def __init__(self, source):
        """The source parameter is assumed to be a NetCDF sww file.
        """

        self.source = source

        self.frame_number = 0

        fin = NetCDFFile(self.source, 'r')

        self.time = num.array(fin.variables['time'][:], float)
        self.last_frame_number = self.time.shape[0] - 1

        self.frames = num.arange(self.last_frame_number+1)

        fin.close()

        self.read_mesh()

        self.quantities = {}

        self.read_quantities()

    def read_mesh(self):
        """ Read and store the mesh data contained within this sww file.
        """
        fin = NetCDFFile(self.source, 'r')

        self.vertices = num.array(fin.variables['volumes'][:], int)

        self.x = x = num.array(fin.variables['x'][:], float)
        self.y = y = num.array(fin.variables['y'][:], float)

        assert len(self.x) == len(self.y)

        self.xmin = num.min(x)
        self.xmax = num.max(x)
        self.ymin = num.min(y)
        self.ymax = num.max(y)

        fin.close()

    def read_quantities(self, frame_number=0):
        """
        Read the quantities contained in this file.
        frame_number is the time index to load.
        """
        assert frame_number >= 0 and frame_number <= self.last_frame_number

        self.frame_number = frame_number

        M = len(self.x)//3

        fin = NetCDFFile(self.source, 'r')

        for q in [n for n in list(fin.variables.keys()) if n != 'x' and n != 'y' and n != 'time' and n != 'volumes' and
                  '_range' not in n and '_c' not in n]:
            # print q
            if len(fin.variables[q].shape) == 1:  # Not a time-varying quantity
                self.quantities[q] = num.ravel(
                    num.array(fin.variables[q][:], float)).reshape(M, 3)
            else:  # Time-varying, get the current timestep data
                self.quantities[q] = num.array(
                    fin.variables[q][self.frame_number], float).reshape(M, 3)
        fin.close()
        return self.quantities

    def get_bounds(self):
        """
            Get the bounding rect around the mesh.
        """
        return [self.xmin, self.xmax, self.ymin, self.ymax]

    def get_last_frame_number(self):
        """
            Return the last loaded frame index.
        """
        return self.last_frame_number

    def get_time(self):
        """
            Get time at the current frame num, in secs.
        """
        return self.time[self.frame_number]


class Write_sww(Write_sts):
    """
        A class to write an SWW file.

        It is domain agnostic, and requires all the data to be fed in
        manually.
    """

    def __init__(self,
                 static_quantities,
                 dynamic_quantities,
                 static_c_quantities=None,
                 dynamic_c_quantities=None):
        """Initialise Write_sww with two (or 4) list af quantity names:

        static_quantities (e.g. elevation or friction):
            Stored once at the beginning of the simulation in a 1D array
            of length number_of_points
        dynamic_quantities (e.g stage):
            Stored every timestep in a 2D array with
            dimensions number_of_points X number_of_timesteps

        static_c_quantities (e.g. elevation_c or friction_c):
            Stored once at the beginning of the simulation in a 1D array
            of length number_of_triangles
        dynamic_c_quantities (e.g stage_c):
            Stored every timestep in a 2D array with
            dimensions number_of_triangles X number_of_timesteps

        """
        self.static_quantities = static_quantities
        self.dynamic_quantities = dynamic_quantities
        self.static_c_quantities = static_c_quantities if static_c_quantities is not None else []
        self.dynamic_c_quantities = dynamic_c_quantities if dynamic_c_quantities is not None else []

        self.store_centroids = False
        if static_c_quantities or dynamic_c_quantities:
            self.store_centroids = True

    def store_header(self,
                     outfile,
                     times,
                     number_of_volumes,
                     number_of_points,
                     institution='Geosciences Australia',
                     description='Generated by ANUGA',
                     smoothing=True,
                     order=1,
                     sww_precision=netcdf_float32,
                     timezone='UTC',
                     verbose=False,
                     zlib=False,
                     complevel=4):
        """Write an SWW file header.

        Writes the first section of the .sww file.

        outfile - the open file that will be written
        times - A list of the time slice times OR a start time
        Note, if a list is given the info will be made relative.
        number_of_volumes - the number of triangles
        number_of_points - the number of vertices in the mesh
        zlib - if True, enable zlib compression on dynamic variables (requires NETCDF4 format)
        complevel - zlib compression level 1-9 (default 4)
        """

        from anuga import get_revision_number
        from anuga import get_revision_date
        from anuga import get_version

        outfile.institution = institution
        outfile.description = description

        # For sww compatibility
        if smoothing is True:
            # Smoothing to be depreciated
            outfile.smoothing = 'Yes'
            outfile.vertices_are_stored_uniquely = 'False'
        else:
            # Smoothing to be depreciated
            outfile.smoothing = 'No'
            outfile.vertices_are_stored_uniquely = 'True'
        outfile.order = order

        try:
            revision_number = get_revision_number()
        except Exception:
            # This will be triggered if the system cannot get the
            # revision number.
            revision_number = None
        # Allow None to be stored as a string
        outfile.revision_number = str(revision_number)

        try:
            revision_date = get_revision_date()
        except Exception:
            # This will be triggered if the system cannot get the
            # revision date.
            revision_date = None
        # Allow None to be stored as a string
        outfile.revision_date = str(revision_date)

        try:
            anuga_version = get_version()
        except Exception:
            # This will be triggered if the system cannot get the
            # version.
            version = None
        # Allow None to be stored as a string
        outfile.anuga_version = str(anuga_version)

        # This is being used to seperate one number from a list.
        # what it is actually doing is sorting lists from numeric arrays.
        if isinstance(times, (list, num.ndarray)):
            number_of_times = len(times)
            times = ensure_numeric(times)
            if number_of_times == 0:
                starttime = 0
            else:
                starttime = times[0]
                times = times - starttime  # Store relative times
        else:
            number_of_times = 0
            starttime = times

        outfile.starttime = starttime
        outfile.timezone = timezone

        # dimension definitions
        outfile.createDimension('number_of_volumes', number_of_volumes)
        outfile.createDimension(
            'number_of_triangle_vertices', number_of_points)
        outfile.createDimension('number_of_vertices', 3)
        outfile.createDimension('numbers_in_range', 2)

        if smoothing is True:
            outfile.createDimension('number_of_points', number_of_points)
            # FIXME(Ole): This will cause sww files for parallel domains to
            # have ghost nodes stored (but not used by triangles).
            # To clean this up, we have to change get_vertex_values and
            # friends in quantity.py (but I can't be bothered right now)
        else:
            outfile.createDimension('number_of_points', 3*number_of_volumes)

        outfile.createDimension('number_of_timesteps', number_of_times)

        # variable definitions
        outfile.createVariable('x', sww_precision, ('number_of_points',))
        outfile.createVariable('y', sww_precision, ('number_of_points',))

        outfile.createVariable('volumes', netcdf_int, ('number_of_volumes',
                                                       'number_of_vertices'))

        for q in self.static_quantities:

            outfile.createVariable(q, sww_precision,
                                   ('number_of_points',))

            outfile.createVariable(q + Write_sww.RANGE, sww_precision,
                                   ('numbers_in_range',))

            # Initialise ranges with small and large sentinels.
            # If this was in pure Python we could have used None sensibly
            outfile.variables[q+Write_sww.RANGE][0] = max_float  # Min
            outfile.variables[q+Write_sww.RANGE][1] = -max_float  # Max

        for q in self.static_c_quantities:
            outfile.createVariable(q, sww_precision,
                                   ('number_of_volumes',))

        self.write_dynamic_quantities(outfile, times, precis=sww_precision,
                                      zlib=zlib, complevel=complevel)

        outfile.sync()

    def store_triangulation(self,
                            outfile,
                            points_utm,
                            volumes,
                            zone=None,
                            new_origin=None,
                            points_georeference=None,
                            verbose=False):
        """
        Store triangulation data in the underlying file.

        Stores the points and triangle indices in the sww file

        outfile Open handle to underlying file.

        new_origin georeference that the points can be set to.

        points_georeference The georeference of the points_utm.

        verbose True if this function is to be verbose.

        new_origin - qa georeference that the points can be set to. (Maybe
        do this before calling this function.)

        points_utm - currently a list or array of the points in UTM.
        points_georeference - the georeference of the points_utm

        How about passing new_origin and current_origin.
        If you get both, do a convertion from the old to the new.

        If you only get new_origin, the points are absolute,
        convert to relative

        if you only get the current_origin the points are relative, store
        as relative.

        if you get no georefs create a new georef based on the minimums of
        points_utm.  (Another option would be to default to absolute)

        Yes, and this is done in another part of the code.
        Probably geospatial.

        If you don't supply either geo_refs, then supply a zone. If not
        the default zone will be used.

        precon:
            header has been called.
        """

        number_of_points = len(points_utm)
        volumes = num.array(volumes, num.int32).reshape(-1, 3)

        points_utm = num.array(points_utm)

        # Given the two geo_refs and the points, do the stuff
        # described in the method header
        # if this is needed else where, pull out as a function
        points_georeference = ensure_geo_reference(points_georeference)
        new_origin = ensure_geo_reference(new_origin)
        if new_origin is None and points_georeference is not None:
            points = points_utm
            geo_ref = points_georeference
        else:
            if new_origin is None:
                new_origin = Geo_reference(zone, min(points_utm[:, 0]),
                                           min(points_utm[:, 1]))
            points = new_origin.change_points_geo_ref(points_utm,
                                                      points_georeference)
            geo_ref = new_origin

        # At this stage I need a georef and points
        # the points are relative to the georef
        geo_ref.write_NetCDF(outfile)

        # This will put the geo ref in the middle
        #geo_ref = Geo_reference(refzone,(max(x)+min(x))/2.0,(max(x)+min(y))/2.)

        x = points[:, 0]
        y = points[:, 1]

        #x = x.astype(netcdf_float32)
        #y = y.astype(netcdf_float32)

        if verbose:
            log.info('------------------------------------------------')
            log.info('More Statistics:')
            log.info('  Extent (/lon):')
            log.info('    x in [%f, %f], len(lat) == %d'
                         % (min(x), max(x), len(x)))
            log.info('    y in [%f, %f], len(lon) == %d'
                         % (min(y), max(y), len(y)))
            # log.info('    z in [%f, %f], len(z) == %d'
            #             % (min(elevation), max(elevation), len(elevation)))
            log.info('geo_ref: %s' % str(geo_ref))
            log.info('------------------------------------------------')

        outfile.variables['x'][:] = x  # - geo_ref.get_xllcorner()
        outfile.variables['y'][:] = y  # - geo_ref.get_yllcorner()

        msg = 'Mismatch between shape of volumes array and (number_of_volumes , 3)'
        assert volumes.shape == outfile.variables['volumes'].shape, msg

        outfile.variables['volumes'][:] = volumes

    def write_dynamic_quantities(self, outfile,
                                 times, precis=netcdf_float32, verbose=False,
                                 zlib=False, complevel=4):
        """
            Write out given quantities to file.

            zlib - if True apply zlib compression (requires NETCDF4 file format)
            complevel - compression level 1-9 (default 4, moderate compression/speed)
        """

        compress_kwargs = {}
        if zlib:
            compress_kwargs = {'zlib': True, 'complevel': complevel}

        for q in self.dynamic_quantities:
            outfile.createVariable(q, precis, ('number_of_timesteps',
                                               'number_of_points'),
                                   **compress_kwargs)
            outfile.createVariable(q + Write_sts.RANGE, precis,
                                   ('numbers_in_range',))

            # Initialise ranges with small and large sentinels.
            # If this was in pure Python we could have used None sensibly
            outfile.variables[q+Write_sts.RANGE][0] = max_float  # Min
            outfile.variables[q+Write_sts.RANGE][1] = -max_float  # Max

        for q in self.dynamic_c_quantities:
            outfile.createVariable(q, precis, ('number_of_timesteps',
                                               'number_of_volumes'),
                                   **compress_kwargs)

        # Doing sts_precision instead of Float gives cast errors.
        outfile.createVariable('time', netcdf_float, ('number_of_timesteps',))

        if isinstance(times, (list, num.ndarray)):
            outfile.variables['time'][:] = times    # Store time relative

        if verbose:
            log.info('------------------------------------------------')
            log.info('Statistics:')
            log.info('    t in [%f, %f], len(t) == %d'
                         % (num.min(times), num.max(times), len(times.flat)))

    def store_parallel_data(self,
                            outfile,
                            number_of_global_triangles,
                            number_of_global_nodes,
                            tri_full_flag=None,
                            tri_l2g=None,
                            node_l2g=None,
                            sww_precision=netcdf_float32,
                            verbose=False):

        # dimension definitions
        #outfile.createDimension('number_of_volumes', number_of_volumes)
        #outfile.createDimension('number_of_vertices', 3)
        #outfile.createDimension('numbers_in_range', 2)

        # print 'store parallel data'
        outfile.number_of_global_triangles = number_of_global_triangles
        outfile.number_of_global_nodes = number_of_global_nodes

        # variable definitions
        outfile.createVariable('tri_l2g',  netcdf_int, ('number_of_volumes',))
        outfile.createVariable('node_l2g', netcdf_int,
                               ('number_of_triangle_vertices',))
        outfile.createVariable(
            'tri_full_flag', netcdf_int, ('number_of_volumes',))

        # print tri_l2g.shape
        # print tri_l2g
        # print outfile.variables['tri_l2g'].shape

        outfile.variables['tri_l2g'][:] = tri_l2g.astype(num.int32)

        # print node_l2g.shape
        # print node_l2g
        # print outfile.variables['node_l2g'].shape

        outfile.variables['node_l2g'][:] = node_l2g.astype(num.int32)

        # print tri_full_flag.shape
        # print tri_full_flag
        # print outfile.variables['tri_full_flag'].shape

        outfile.variables['tri_full_flag'][:] = tri_full_flag.astype(num.int32)

    def store_static_quantities(self,
                                outfile,
                                sww_precision=num.float32,
                                verbose=False,
                                **quant):
        """
        Write the static quantity info.

        **quant is extra keyword arguments passed in. These must be
          the numpy arrays to be stored in the sww file at each timestep.

        The argument sww_precision allows for storing as either
        * single precision (default): num.float32
        * double precision: num.float64 or float

        Precondition:
            store_triangulation and
            store_header have been called.
        """

        # The dictionary quant must contain numpy arrays for each name.
        # These will typically be quantities from Domain such as friction
        #
        # Arrays not listed in static_quantitiues will be ignored, silently.
        #
        # This method will also write the ranges for each quantity,
        # e.g. stage_range, xmomentum_range and ymomentum_range
        for q in self.static_quantities:
            if q not in quant:
                msg = 'Values for quantity %s was not specified in ' % q
                msg += 'store_quantities so they cannot be stored.'
                raise NewQuantity(msg)
            else:
                q_values = ensure_numeric(quant[q])

                x = q_values.astype(sww_precision)
                outfile.variables[q][:] = x

                # This populates the _range values
                outfile.variables[q + Write_sww.RANGE][0] = num.min(x)
                outfile.variables[q + Write_sww.RANGE][1] = num.max(x)

        # FIXME: Hack for backwards compatibility with old viewer
        # if 'elevation' in self.static_quantities:
        #    outfile.variables['z'][:] = outfile.variables['elevation'][:]

    def store_static_quantities_centroid(self,
                                         outfile,
                                         sww_precision=num.float32,
                                         verbose=False,
                                         **quant):
        """
        Write the static centroid quantity info.

        **quant is extra keyword arguments passed in. These must be
          the numpy arrays to be stored in the sww file at each timestep.

        The argument sww_precision allows for storing as either
        * single precision (default): num.float32
        * double precision: num.float64 or float

        Precondition:
            store_triangulation and
            store_header have been called.
        """

        # The dictionary quant must contain numpy arrays for each name.
        # These will typically be quantities from Domain such as friction
        #
        # Arrays not listed in static_quantitiues will be ignored, silently.
        #
        # This method will also write the ranges for each quantity,
        # e.g. stage_range, xmomentum_range and ymomentum_range

        # print outfile.variables.keys()
        # print self.static_c_quantities

        for q in self.static_c_quantities:
            if q not in quant:
                msg = 'Values for quantity %s was not specified in ' % q
                msg += 'store_quantities so they cannot be stored.'
                raise NewQuantity(msg)
            else:
                q_values = ensure_numeric(quant[q])

                x = q_values.astype(sww_precision)
                outfile.variables[q][:] = x

    def store_quantities(self,
                         outfile,
                         sww_precision=num.float32,
                         slice_index=None,
                         time=None,
                         verbose=False,
                         **quant):
        """
        Write the quantity info at each timestep.

        **quant is extra keyword arguments passed in. These must be
          the numpy arrays to be stored in the sww file at each timestep.

        if the time array is already been built, use the slice_index
        to specify the index.

        Otherwise, use time to increase the time dimension

        Maybe make this general, but the viewer assumes these quantities,
        so maybe we don't want it general - unless the viewer is general

        The argument sww_precision allows for storing as either
        * single precision (default): num.float32
        * double precision: num.float64 or float

        Precondition:
            store_triangulation and
            store_header have been called.
        """

        if time is not None:
            file_time = outfile.variables['time']
            slice_index = len(file_time)
            # check if time already saved as in check pointing
            if slice_index > 0:
                if time <= file_time[slice_index-1]:
                    check = numpy.where(numpy.abs(file_time[:]-time) < 1.0e-14)
                    slice_index = int(check[0][0])
            file_time[slice_index] = time
        else:
            # Has to be cast in case it was numpy.int
            slice_index = int(slice_index)

        # Write the named dynamic quantities
        # The dictionary quant must contain numpy arrays for each name.
        # These will typically be the conserved quantities from Domain
        # (Typically stage,  xmomentum, ymomentum).
        #
        # Arrays not listed in dynamic_quantitiues will be ignored, silently.
        #
        # This method will also write the ranges for each quantity,
        # e.g. stage_range, xmomentum_range and ymomentum_range
        for q in self.dynamic_quantities:
            if q not in quant:
                msg = 'Values for quantity %s was not specified in ' % q
                msg += 'store_quantities so they cannot be stored.'
                raise NewQuantity(msg)
            else:
                q_values = ensure_numeric(quant[q])

                q_retyped = q_values.astype(sww_precision)
                outfile.variables[q][slice_index] = q_retyped

                # This updates the _range values
                q_range = outfile.variables[q + Write_sww.RANGE][:]
                q_values_min = num.min(q_values)
                if q_values_min < q_range[0]:
                    outfile.variables[q + Write_sww.RANGE][0] = q_values_min
                q_values_max = num.max(q_values)
                if q_values_max > q_range[1]:
                    outfile.variables[q + Write_sww.RANGE][1] = q_values_max

        return slice_index

    def store_quantities_centroid(self,
                                  outfile,
                                  sww_precision=num.float32,
                                  slice_index=None,
                                  verbose=False,
                                  **quant):
        """
        Write the quantity centroid info at each timestep.

        **quant is extra keyword arguments passed in. These must be
          the numpy arrays to be stored in the sww file at each timestep.

        if the time array is already been built, use the slice_index
        to specify the index.

        Otherwise, use time to increase the time dimension

        Maybe make this general, but the viewer assumes these quantities,
        so maybe we don't want it general - unless the viewer is general

        The argument sww_precision allows for storing as either
        * single precision (default): num.float32
        * double precision: num.float64 or float

        Precondition:
            store_triangulation and
            store_header have been called.
        """

        assert slice_index is not None, 'slice_index should be set in store_quantities'

        # Write the named dynamic quantities
        # The dictionary quant must contain numpy arrays for each name.
        # These will typically be the conserved quantities from Domain
        # (Typically stage,  xmomentum, ymomentum).
        #
        # Arrays not listed in dynamic_quantitiues will be ignored, silently.
        #
        # This method will also write the ranges for each quantity,
        # e.g. stage_range, xmomentum_range and ymomentum_range

        # print 50*"="
        # print quant
        # print self.dynamic_c_quantities

        for q in self.dynamic_c_quantities:
            if q not in quant:
                msg = 'Values for quantity %s was not specified in ' % q
                msg += 'store_quantities so they cannot be stored.'
                raise NewQuantity(msg)
            else:
                q_values = ensure_numeric(quant[q])

                q_retyped = q_values.astype(sww_precision)
                outfile.variables[q][slice_index] = q_retyped

    def verbose_quantities(self, outfile):
        log.info('------------------------------------------------')
        log.info('More Statistics:')
        for q in self.dynamic_quantities:
            log.info('  %s in [%f, %f]'
                         % (q, outfile.variables[q+Write_sww.RANGE][0],
                            outfile.variables[q+Write_sww.RANGE][1]))
        log.info('------------------------------------------------')


def extent_sww(file_name):
    """Read in an sww file, then get its extents

    Input:
    file_name - the sww file

    Output:
    A list: [min(x),max(x),min(y),max(y),min(stage.flat),max(stage.flat)]
    """

    # Get NetCDF
    fid = NetCDFFile(file_name, netcdf_mode_r)

    # Get the variables
    x = fid.variables['x'][:]
    y = fid.variables['y'][:]
    stage = fid.variables['stage'][:]

    fid.close()

    return [min(x), max(x), min(y), max(y), num.min(stage), num.max(stage)]


def Xload_sww_as_domain(filename, boundary=None, t=None,
                       fail_if_NaN=True, NaN_filler=0,
                       verbose=False, very_verbose=False):
    """
    Load an sww file into a domain.


    DEPRECATE THIS. It is not robust and doesn't for instance provide a proper
    boundary map (as this information isn't currently stored in sww files)


    Usage: domain = load_sww_as_domain('file.sww',
                        t=time (default = last time in file))

    Boundary is not recommended if domain.smooth is not selected, as it
    uses unique coordinates, but not unique boundaries. This means that
    the boundary file will not be compatable with the coordinates, and will
    give a different final boundary, or crash.
    """

    from anuga.shallow_water.shallow_water_domain import Domain

    # initialise NaN.
    NaN = 9.969209968386869e+036

    if verbose:
        log.info('Reading from %s' % filename)

    fid = NetCDFFile(filename, netcdf_mode_r)    # Open existing file for read
    time = fid.variables['time'][:]       # Timesteps
    if t is None:
        t = time[-1]
    time_interp = get_time_interp(time, t)

    # Get the variables as numeric arrays
    x = fid.variables['x'][:]                   # x-coordinates of vertices
    y = fid.variables['y'][:]                   # y-coordinates of vertices
    # elevation = fid.variables['elevation']      # Elevation
    # stage = fid.variables['stage']              # Water level
    # xmomentum = fid.variables['xmomentum']      # Momentum in the x-direction
    # ymomentum = fid.variables['ymomentum']      # Momentum in the y-direction

    starttime = float(fid.starttime)
    #starttime = fid.starttime[0]
    volumes = fid.variables['volumes'][:]       # Connectivity
    coordinates = num.transpose(num.asarray([x.tolist(), y.tolist()]))
    # FIXME (Ole): Something like this might be better:
    #                 concatenate((x, y), axis=1)
    # or              concatenate((x[:,num.newaxis], x[:,num.newaxis]), axis=1)

    dynamic_quantities = []
    interpolated_quantities = {}
    static_quantities = []

    # get geo_reference
    try:                             # sww files don't have to have a geo_ref
        geo_reference = Geo_reference(NetCDFObject=fid)
    except Exception:  # AttributeError, e:
        geo_reference = None

    if verbose:
        log.info('    getting quantities')

    for quantity in list(fid.variables.keys()):
        dimensions = fid.variables[quantity].dimensions
        if 'number_of_timesteps' in dimensions:
            dynamic_quantities.append(quantity)
            interpolated_quantities[quantity] = \
                interpolated_quantity(fid.variables[quantity][:], time_interp)
        else:
            static_quantities.append(quantity)

    # print static_quantities
    # print dynamic_quantities

    try:
        dynamic_quantities.remove('stage_c')
        dynamic_quantities.remove('xmomentum_c')
        dynamic_quantities.remove('ymomentum_c')
        dynamic_quantities.remove('elevation_c')
        dynamic_quantities.remove('friction_c')
    except ValueError:
        pass

    try:
        static_quantities.remove('elevation_c')
        static_quantities.remove('friction_c')
    except ValueError:
        pass

    static_quantities.remove('x')
    static_quantities.remove('y')
    # other_quantities.remove('z')
    static_quantities.remove('volumes')
    try:
        static_quantities.remove('stage_range')
        static_quantities.remove('xmomentum_range')
        static_quantities.remove('ymomentum_range')
        static_quantities.remove('elevation_range')
        static_quantities.remove('friction_range')
    except ValueError:
        pass

    dynamic_quantities.remove('time')

    if verbose:
        log.info('    building domain')

    #    From domain.Domain:
    #    domain = Domain(coordinates, volumes,\
    #                    conserved_quantities = conserved_quantities,\
    #                    other_quantities = other_quantities,zone=zone,\
    #                    xllcorner=xllcorner, yllcorner=yllcorner)

    # From shallow_water.Domain:
    coordinates = coordinates.tolist()
    volumes = volumes.tolist()
    # FIXME:should this be in mesh? (peter row)
    if fid.smoothing == 'Yes':
        unique = False
    else:
        unique = True

    if unique:
        coordinates, volumes, boundary = weed(coordinates, volumes, boundary)

    try:
        domain = Domain(coordinates, volumes, boundary,
                        starttime=(float(starttime) + float(t)))
    except AssertionError as e:
        fid.close()
        msg = 'Domain could not be created: %s. ' \
              'Perhaps use "fail_if_NaN=False and NaN_filler = ..."' % e
        raise DataDomainError(msg)

    if boundary is not None:
        domain.boundary = boundary

    domain.geo_reference = geo_reference

    for quantity in static_quantities:
        try:
            NaN = fid.variables[quantity].missing_value
        except AttributeError:
            pass                       # quantity has no missing_value number
        X = fid.variables[quantity][:]
        if very_verbose:
            log.info('       %s' % str(quantity))
            log.info('        NaN = %s' % str(NaN))
            log.info('        max(X)')
            log.info('       %s' % str(max(X)))
            log.info('        max(X)==NaN')
            log.info('       %s' % str(max(X) == NaN))
            log.info('')
        if max(X) == NaN or min(X) == NaN:
            if fail_if_NaN:
                msg = 'quantity "%s" contains no_data entry' % quantity
                raise DataMissingValuesError(msg)
            else:
                data = (X != NaN)
                X = (X*data) + (data == 0)*NaN_filler
        if unique:
            X = num.resize(X, (len(X)//3, 3))
        domain.set_quantity(quantity, X)
    #
    for quantity in dynamic_quantities:
        try:
            NaN = fid.variables[quantity].missing_value
        except AttributeError:
            pass                       # quantity has no missing_value number
        X = interpolated_quantities[quantity]
        if very_verbose:
            log.info('       %s' % str(quantity))
            log.info('        NaN = %s' % str(NaN))
            log.info('        max(X)')
            log.info('       %s' % str(max(X)))
            log.info('        max(X)==NaN')
            log.info('       %s' % str(max(X) == NaN))
            log.info('')
        if max(X) == NaN or min(X) == NaN:
            if fail_if_NaN:
                msg = 'quantity "%s" contains no_data entry' % quantity
                raise DataMissingValuesError(msg)
            else:
                data = (X != NaN)
                X = (X*data) + (data == 0)*NaN_filler
        if unique:
            X = num.resize(X, (X.shape[0]//3, 3))
        domain.set_quantity(quantity, X)

    fid.close()

    return domain


def get_mesh_and_quantities_from_file(filename,
                                      quantities=None,
                                      verbose=False):
    """Get and rebuild mesh structure and associated quantities from sww file

    Input:
        filename - Name os sww file
        quantities - Names of quantities to load

    Output:
        mesh - instance of class Interpolate
               (including mesh and interpolation functionality)
        quantities - arrays with quantity values at each mesh node or
                    each triangle vertex. (depending on whethr stored as smooth or not)
        time - vector of stored timesteps

    This function is used by e.g.:
        get_interpolated_quantities_at_polyline_midpoints
    """

    # FIXME (Ole): Maybe refactor filefunction using this more fundamental code.

    import types
    from anuga.abstract_2d_finite_volumes.neighbour_mesh import Mesh

    if verbose:
        log.info('Reading from %s' % filename)

    fid = NetCDFFile(filename, netcdf_mode_r)    # Open existing file for read
    time = fid.variables['time'][:]    # Time vector
    #time += fid.starttime[0]
    time += fid.starttime

    # Get the variables as numeric arrays
    x = fid.variables['x'][:]                   # x-coordinates of nodes
    y = fid.variables['y'][:]                   # y-coordinates of nodes

    elevation = fid.variables['elevation'][:]   # Elevation
    stage = fid.variables['stage'][:]           # Water level
    xmomentum = fid.variables['xmomentum'][:]   # Momentum in the x-direction
    ymomentum = fid.variables['ymomentum'][:]   # Momentum in the y-direction

    # Mesh (nodes (Mx2), triangles (Nx3))
    nodes = num.concatenate((x[:, num.newaxis], y[:, num.newaxis]), axis=1)
    triangles = fid.variables['volumes'][:]

    # Get geo_reference
    try:
        geo_reference = Geo_reference(NetCDFObject=fid)
    except Exception:  # AttributeError, e:
        # Sww files don't have to have a geo_ref
        geo_reference = None

    if verbose:
        log.info('    building mesh from sww file %s' % filename)

    boundary = None

    # FIXME (Peter Row): Should this be in mesh?
    if fid.smoothing != 'Yes':
        nodes = nodes.tolist()
        triangles = triangles.tolist()
        nodes, triangles, boundary = weed(nodes, triangles, boundary)

    try:
        mesh = Mesh(nodes, triangles, boundary, geo_reference=geo_reference)
    except AssertionError as e:
        fid.close()
        msg = 'Domain could not be created: %s. "' % e
        raise DataDomainError(msg)

    def gather(quantity):

        shape = quantity.shape

        def my_num_add_at(a, indices, b):
            """
            Use the numpy add.at operation if it is available, (numpy version >1.8)
            otherwise just use a quick and dirty implementation via a python loop
            """

            try:
                num.add.at(a, indices, b)
            except AttributeError:
                n_ids = len(indices)
                b_array = num.zeros_like(indices, dtype=float)
                b_array[:] = b

                for n in range(n_ids):
                    a[indices[n]] = a[indices[n]] + b_array[n]

        if len(shape) == 2:
            # time array
            if shape[1] == len(mesh.nodes):
                return quantity
            # need to calculate unique vertex values
            n_time = shape[0]
            n_nodes = len(mesh.nodes)

            mesh_ids = mesh.triangles.ravel()
            temp_uv = num.zeros((n_time, n_nodes))

            count_uv = num.zeros(len(mesh.nodes))
            my_num_add_at(count_uv, mesh_ids, 1)

            for i in range(n_time):
                my_num_add_at(temp_uv[i, :], mesh_ids, quantity[i, :])
                temp_uv[i, :] = temp_uv[i, :]/count_uv

        elif len(shape) == 1:
            # non time array
            if shape[0] == len(mesh.nodes):
                return quantity

            mesh_ids = mesh.triangles.ravel()
            temp_uv = num.zeros(len(mesh.nodes))

            count_uv = num.zeros(len(mesh.nodes))
            my_num_add_at(count_uv, mesh_ids, 1)
            my_num_add_at(temp_uv, mesh_ids, quantity)

        else:
            raise Exception

        return temp_uv

    quantities = {}

    if fid.smoothing != 'Yes':
        quantities['elevation'] = elevation[:]
        quantities['stage'] = stage[:]
        quantities['xmomentum'] = xmomentum[:]
        quantities['ymomentum'] = ymomentum[:]
    else:
        quantities['elevation'] = gather(elevation)
        quantities['stage'] = gather(stage)
        quantities['xmomentum'] = gather(xmomentum)
        quantities['ymomentum'] = gather(ymomentum)

    fid.close()

    return mesh, quantities, time


def get_time_interp(time, t=None):
    """Finds the ratio and index for time interpolation.
        time is an array of time steps
        t is the sample time.
        returns a tuple containing index into time, and ratio
    """
    if t is None:
        t = time[-1]
        index = -1
        ratio = 0.
    else:
        T = time
        tau = t
        index = 0
        msg = 'Time interval derived from file %s [%s:%s]' \
              % ('FIXMEfilename', T[0], T[-1])
        msg += ' does not match model time: %s' % tau
        if tau < time[0]:
            raise DataTimeError(msg)
        if tau > time[-1]:
            raise DataTimeError(msg)
        while tau > time[index]:
            index += 1
        while tau < time[index]:
            index -= 1
        if tau == time[index]:
            # Protect against case where tau == time[-1] (last time)
            # - also works in general when tau == time[i]
            ratio = 0
        else:
            # t is now between index and index+1
            ratio = (tau - time[index])/(time[index+1] - time[index])

    return (index, ratio)


def interpolated_quantity(saved_quantity, time_interp):
    """Interpolate a quantity with respect to time.

    saved_quantity  the quantity to interpolate
    time_interp     (index, ratio)

    Returns a vector of interpolated values.
    """

    index, ratio = time_interp

    Q = saved_quantity

    if ratio > 0:
        q = (1-ratio)*Q[index] + ratio*Q[index+1]
    else:
        q = Q[index]

    # Return vector of interpolated values
    return q


def weed(coordinates, volumes, boundary=None):
    """ Excise all duplicate points.
    """
    if isinstance(coordinates, num.ndarray):
        coordinates = coordinates.tolist()
    if isinstance(volumes, num.ndarray):
        volumes = volumes.tolist()

    unique = False
    point_dict = {}
    same_point = {}
    for i in range(len(coordinates)):
        point = tuple(coordinates[i])
        if point in point_dict:
            unique = True
            same_point[i] = point
            # to change all point i references to point j
        else:
            point_dict[point] = i
            same_point[i] = point

    coordinates = []
    i = 0
    for point in list(point_dict.keys()):
        point = tuple(point)
        coordinates.append(list(point))
        point_dict[point] = i
        i += 1

    for volume in volumes:
        for i in range(len(volume)):
            index = volume[i]
            if index > -1:
                volume[i] = point_dict[same_point[index]]

    new_boundary = {}
    if boundary is not None:
        for segment in list(boundary.keys()):
            point0 = point_dict[same_point[segment[0]]]
            point1 = point_dict[same_point[segment[1]]]
            label = boundary[segment]
            # FIXME should the bounday attributes be concaterated
            # ('exterior, pond') or replaced ('pond')(peter row)

            if (point0, point1) in new_boundary:
                new_boundary[(point0, point1)] = new_boundary[(point0, point1)]

            elif (point1, point0) in new_boundary:
                new_boundary[(point1, point0)] = new_boundary[(point1, point0)]
            else:
                new_boundary[(point0, point1)] = label

        boundary = new_boundary

    return coordinates, volumes, boundary


def sww_files_are_equal(filename1, filename2):
    """Read and compare numerical values of two sww files: filename1 and filename2

    If they are identical (up to a tolerance) the return value is True
    If anything substantial is different, the return value is False.
    """

    import anuga.utilities.plot_utils as util

    if not (filename1.endswith('.sww') and filename2.endswith('.sww')):
        msg = f'Filenames {filename1} and {filename2} must both end with .sww'
        raise Exception(msg)


    domain1_v = util.get_output(filename1)
    domain1_c = util.get_centroids(domain1_v)

    domain2_v = util.get_output(filename2)
    domain2_c = util.get_centroids(domain2_v)

    if not num.allclose(domain1_c.stage, domain2_c.stage):
        return False

    if not num.allclose(domain1_c.xmom, domain2_c.xmom):
        return False

    if not num.allclose(domain1_c.ymom, domain2_c.ymom):
        return False

    if not num.allclose(domain1_c.xvel, domain2_c.xvel):
        return False

    if not num.allclose(domain1_c.yvel, domain2_c.yvel):
        return False

    if not num.allclose(domain1_v.x, domain2_v.x):
        return False

    if not num.allclose(domain1_v.y, domain2_v.y):
        return False

    # Otherwise, they are deemed to be identical
    return True

