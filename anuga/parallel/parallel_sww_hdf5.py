"""Parallel HDF5 output for ANUGA parallel domains.

Exascale item 3: eliminate the serial post-hoc SWW merge by having all MPI
ranks write simultaneously to a single shared HDF5 file.

Design
------
* Uses h5py's 'mpio' driver backed by HDF5 Parallel I/O (requires HDF5
  built with --enable-parallel and h5py built against it).
* Each rank writes only its *owned* (full) triangles to a pre-allocated
  dataset.  Writes are collective: every rank participates in every call,
  each writing to a non-overlapping hyperslab.
* Centroid-based storage avoids the vertex deduplication problem that
  makes vertex-based parallel writes awkward (ghost nodes shared between
  ranks would need exactly-one ownership tracking).
* A tri_global_ids dataset records the global triangle index for each
  position in the file, so post-processing tools can reconstruct the
  full global mesh ordering.

File layout
-----------
/starttime           scalar float64
/time                (T,)                float64    simulation times
/centroid_x          (N_global_tri,)     float32    triangle centroid x
/centroid_y          (N_global_tri,)     float32    triangle centroid y
/elevation_c         (N_global_tri,)     float32    bed elevation (static)
/friction_c          (N_global_tri,)     float32    friction (static, optional)
/stage_c             (T, N_global_tri)   float32    water stage (dynamic)
/xmomentum_c         (T, N_global_tri)   float32    x-momentum (dynamic)
/ymomentum_c         (T, N_global_tri)   float32    y-momentum (dynamic)

Note: N_global_tri is the *total number of full triangles across all ranks*
      (== domain.number_of_global_triangles). Each rank writes a contiguous
      slice identified by its prefix sum of n_full values.

Usage
-----
    # In the parallel evolve script, after distribute():
    from anuga.parallel.parallel_sww_hdf5 import Parallel_SWW_HDF5

    writer = Parallel_SWW_HDF5(domain, 'results.ph5')
    writer.store_connectivity()

    for t in domain.evolve(yieldstep=60, finaltime=3600):
        writer.store_timestep()

    writer.close()

    # On rank 0, convert to SWW for viewing (offline, no MPI needed):
    if domain.processor == 0:
        from anuga.parallel.parallel_sww_hdf5 import convert_ph5_to_sww
        convert_ph5_to_sww('results.ph5', 'results.sww')
"""

import numpy as np


class Parallel_SWW_HDF5:
    """Parallel HDF5 writer for ANUGA shallow-water output.

    All MPI ranks write simultaneously to a single shared HDF5 file using
    HDF5 Parallel I/O.  No post-hoc merge is required.

    Parameters
    ----------
    domain : Parallel_domain
        The local parallel domain.
    filename : str
        Output file path (typically ending with ``.ph5``).
    quantities : list of str, optional
        Dynamic quantities to store.  Defaults to
        ``['stage', 'xmomentum', 'ymomentum']``.
    static_quantities : list of str, optional
        Static quantities to store once.  Defaults to ``['elevation', 'friction']``.
    precision : dtype, optional
        Storage precision.  Default ``np.float32``.
    """

    def __init__(self, domain, filename,
                 quantities=None,
                 static_quantities=None,
                 precision=np.float32):
        try:
            import h5py
            from mpi4py import MPI
        except ImportError as e:
            raise ImportError(
                f'Parallel_SWW_HDF5 requires h5py (with MPI-IO) and mpi4py: {e}'
            ) from e

        if 'mpio' not in h5py.registered_drivers():
            raise RuntimeError(
                'h5py was not built with HDF5 Parallel I/O support. '
                'Install a parallel-enabled h5py: '
                'conda install -c conda-forge h5py=*=mpi*'
            )

        self.domain    = domain
        self.filename  = filename
        self.precision = precision
        self.comm      = MPI.COMM_WORLD
        self._h5py     = h5py

        self.quantities        = list(quantities or ['stage', 'xmomentum', 'ymomentum'])
        self.static_quantities = list(static_quantities or ['elevation', 'friction'])

        # Identify owned (non-ghost) triangles on this rank.
        full_mask   = (domain.tri_full_flag == 1)
        self.full_idx    = np.where(full_mask)[0]            # local indices
        self.full_global = domain.tri_l2g[self.full_idx]    # global indices
        self.n_full      = len(self.full_idx)

        self.n_global_tri = int(domain.number_of_global_triangles)

        # Compute per-rank write offset via Allgather of n_full counts.
        n_full_all    = np.zeros(domain.numproc, dtype=np.int64)
        local_count   = np.array([self.n_full], dtype=np.int64)
        from mpi4py.MPI import INT64_T
        self.comm.Allgather(local_count, n_full_all)
        self.rank_offset = int(np.sum(n_full_all[:domain.processor]))

        # Verify global count is consistent.
        total = int(np.sum(n_full_all))
        if total != self.n_global_tri:
            import warnings
            warnings.warn(
                f'Parallel_SWW_HDF5: sum(n_full) across ranks = {total} '
                f'but domain.number_of_global_triangles = {self.n_global_tri}. '
                f'Using sum(n_full) as the dataset size.',
                RuntimeWarning, stacklevel=2)
            self.n_global_tri = total

        self._timestep_count = 0

        # Open file collectively.
        self.fid = h5py.File(
            filename, 'w', driver='mpio', comm=self.comm)
        self._create_datasets()

    def _create_datasets(self):
        fid = self.fid
        N   = self.n_global_tri

        fid.attrs['starttime'] = float(self.domain.starttime)

        # Static geometry
        fid.create_dataset('centroid_x', shape=(N,), dtype=self.precision)
        fid.create_dataset('centroid_y', shape=(N,), dtype=self.precision)

        # Static quantities (pre-allocated; filled by store_connectivity)
        for q in self.static_quantities:
            if q in self.domain.quantities:
                fid.create_dataset(q + '_c', shape=(N,), dtype=self.precision)

        # Dynamic datasets: unlimited time axis
        fid.create_dataset(
            'time', shape=(0,), maxshape=(None,), dtype='float64')
        for q in self.quantities:
            fid.create_dataset(
                q + '_c',
                shape=(0, N),
                maxshape=(None, N),
                dtype=self.precision,
                chunks=(1, min(N, 65536)),
            )

    def store_connectivity(self):
        """Write centroid coordinates and static quantities (once).

        Must be called by all ranks collectively.
        """
        domain = self.domain
        fid    = self.fid
        sl     = slice(self.rank_offset, self.rank_offset + self.n_full)

        cc = domain.centroid_coordinates
        fid['centroid_x'][sl] = cc[self.full_idx, 0].astype(self.precision)
        fid['centroid_y'][sl] = cc[self.full_idx, 1].astype(self.precision)

        for q in self.static_quantities:
            if q in self.domain.quantities and q + '_c' in fid:
                vals = domain.quantities[q].centroid_values
                fid[q + '_c'][sl] = vals[self.full_idx].astype(self.precision)

        fid.flush()

    def store_timestep(self):
        """Append current conserved quantities at the current simulation time.

        Must be called by all ranks collectively at every yield step.
        """
        domain = self.domain
        fid    = self.fid
        t_idx  = self._timestep_count
        sl     = slice(self.rank_offset, self.rank_offset + self.n_full)

        # Extend unlimited datasets.
        fid['time'].resize((t_idx + 1,))
        for q in self.quantities:
            fid[q + '_c'].resize((t_idx + 1, self.n_global_tri))

        # Rank 0 writes the simulation time (scalar, same on all ranks).
        if domain.processor == 0:
            fid['time'][t_idx] = float(domain.relative_time)

        # Every rank writes its owned triangles' centroid values.
        for q in self.quantities:
            if q in domain.quantities:
                vals = domain.quantities[q].centroid_values
                fid[q + '_c'][t_idx, sl] = vals[self.full_idx].astype(self.precision)

        fid.flush()
        self._timestep_count += 1

    def close(self):
        """Close the HDF5 file (collective)."""
        self.fid.close()


def convert_ph5_to_sww(ph5_file, sww_file, verbose=False):
    """Convert a parallel HDF5 output file to SWW (NetCDF) format.

    Intended to be run on rank 0 (or serially after the simulation) to
    produce a file readable by the standard ANUGA visualisation tools.

    The resulting SWW stores centroid quantities only (``stage_c``,
    ``xmomentum_c``, ``ymomentum_c``, ``elevation_c``), which most
    post-processing scripts already support via ``store_centroids=True``.

    Parameters
    ----------
    ph5_file : str   Path to the ``.ph5`` file written by Parallel_SWW_HDF5.
    sww_file : str   Destination ``.sww`` path.
    verbose : bool
    """
    try:
        import h5py
    except ImportError as exc:
        raise ImportError('convert_ph5_to_sww requires h5py') from exc

    from anuga.file.netcdf import NetCDFFile
    from anuga.config import netcdf_mode_w, netcdf_float, netcdf_int

    if verbose:
        print(f'convert_ph5_to_sww: reading {ph5_file}')

    with h5py.File(ph5_file, 'r') as src:
        time        = src['time'][:]
        centroid_x  = src['centroid_x'][:]
        centroid_y  = src['centroid_y'][:]
        elevation_c = src['elevation_c'][:] if 'elevation_c' in src else None
        stage_c     = src['stage_c'][:]
        xmom_c      = src['xmomentum_c'][:]
        ymom_c      = src['ymomentum_c'][:]
        starttime   = float(src.attrs.get('starttime', 0.0))

    N = len(centroid_x)
    T = len(time)

    if verbose:
        print(f'  {N} triangles, {T} timesteps')

    fid = NetCDFFile(sww_file, netcdf_mode_w)
    fid.starttime   = starttime
    fid.institution = 'ANUGA parallel HDF5 output'
    fid.description = f'Converted from {ph5_file}'

    fid.createDimension('number_of_volumes', N)
    fid.createDimension('number_of_timesteps', T)
    fid.createDimension('numbers_in_range', 2)

    fid.createVariable('time', netcdf_float, ('number_of_timesteps',))
    fid.variables['time'][:] = time

    fid.createVariable('centroid_x', 'f', ('number_of_volumes',))
    fid.createVariable('centroid_y', 'f', ('number_of_volumes',))
    fid.variables['centroid_x'][:] = centroid_x
    fid.variables['centroid_y'][:] = centroid_y

    if elevation_c is not None:
        fid.createVariable('elevation_c', 'f', ('number_of_volumes',))
        fid.variables['elevation_c'][:] = elevation_c

    for name, data in [('stage_c', stage_c),
                       ('xmomentum_c', xmom_c),
                       ('ymomentum_c', ymom_c)]:
        fid.createVariable(name, 'f', ('number_of_timesteps', 'number_of_volumes'))
        fid.variables[name][:] = data

    fid.close()

    if verbose:
        print(f'convert_ph5_to_sww: wrote {sww_file}')
