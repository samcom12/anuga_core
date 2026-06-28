"""Class Parallel_shallow_water_domain -
2D triangular domains for finite-volume computations of
the shallow water equation, with extra structures to allow
communication between other Parallel_domains and itself

This module contains a specialisation of class Domain
from module shallow_water.py

Ole Nielsen, Stephen Roberts, Duncan Gray, Christopher Zoppou
Geoscience Australia, 2004-2005

"""

from anuga import Domain

from . import parallel_generic_communications as generic_comms

import anuga.utilities.parallel_abstraction as pypar

#from anuga.abstract_2d_finite_volumes.neighbour_mesh import Mesh

import numpy as num
from os.path import join


#Import matplotlib



class Parallel_domain(Domain):

    def __init__(self, coordinates, vertices,
                 boundary=None,
                 full_send_dict=None,
                 ghost_recv_dict=None,
                 number_of_full_nodes=None,
                 number_of_full_triangles=None,
                 geo_reference=None,
                 processor=None,
                 numproc=None,
                 number_of_global_triangles=None,
                 number_of_global_nodes=None,
                 s2p_map=None,
                 p2s_map=None,
                 tri_l2g=None,
                 node_l2g=None,
                 ghost_layer_width=2,
                 timestep_mode='global',
                 overlap_ghost_exchange=False,
                 local_timestep_safety_factor=0.9):
        """Parallel shallow-water domain.

        Parameters
        ----------
        timestep_mode : str, optional
            CFL timestep synchronisation strategy.

            ``'global'`` (default)
                Standard ``MPI_Allreduce(MIN)`` across all ranks — correct on
                all topologies but latency scales as O(log P).

            ``'hierarchical'``
                Two-level reduction: intra-node ``MPI_Reduce(MIN)`` via shared
                memory (~10 ns), then inter-node ``MPI_Allreduce(MIN)`` across
                node roots only.  Identical result to ``'global'``; latency
                constant drops by P/N where N is the number of nodes.
                Requires ``MPI_COMM_TYPE_SHARED`` (OpenMPI ≥ 1.8, MPICH ≥ 3.1,
                Cray MPICH ≥ 7).  Falls back to ``'global'`` if unsupported.

            ``'local'``
                No global synchronisation.  Each rank advances by its own local
                CFL timestep multiplied by ``local_timestep_safety_factor``.
                Scales as O(1) in P.  Use only when the mesh is quasi-uniform
                so local dt values across ranks do not diverge by more than
                ~10%.  All ranks still synchronise at ``yieldstep`` boundaries.

        overlap_ghost_exchange : bool, optional
            If ``True``, split the ghost exchange into a non-blocking start
            (posted right after ``update_conserved_quantities``) and a blocking
            finish (called at the start of the next ``distribute_to_vertices``).
            The network transfer then overlaps with ``apply_fractional_steps``,
            hiding the communication latency behind useful computation.

            The values sent are the post-flux, pre-fractional-step conserved
            quantities.  The resulting temporal error is O(dt × h²) — higher
            order than the O(dt + h²) scheme truncation error — and is safe for
            practical use (same approximation used by ADCIRC at scale).

            Overlap is only active for the ``'euler'`` timestepping method;
            RK2/RK3 fall back to the standard blocking exchange.

        local_timestep_safety_factor : float, optional
            Extra multiplier applied to the local CFL timestep in
            ``timestep_mode='local'``.  Default 0.9 (10 % margin).
        """

        if processor is None:
            processor = pypar.rank()
        if numproc is None:
            numproc = pypar.size()

        Domain.__init__(self,
                        coordinates,
                        vertices,
                        boundary,
                        full_send_dict=full_send_dict,
                        ghost_recv_dict=ghost_recv_dict,
                        processor=processor,
                        numproc=numproc,
                        number_of_full_nodes=number_of_full_nodes,
                        number_of_full_triangles=number_of_full_triangles,
                        geo_reference=geo_reference,
                        ghost_layer_width=ghost_layer_width)

        self.parallel = True

        if number_of_full_nodes is not None:
            self.number_of_full_nodes_tmp = number_of_full_nodes
        else:
            self.number_of_full_nodes_tmp = self.get_number_of_nodes()

        if number_of_full_triangles is not None:
            self.number_of_full_triangles_tmp = number_of_full_triangles
        else:
            self.number_of_full_triangles_tmp = self.get_number_of_triangles()

        generic_comms.setup_buffers(self)

        self.global_name = 'domain'

        self.number_of_global_triangles = number_of_global_triangles
        self.number_of_global_nodes = number_of_global_nodes

        self.s2p_map = None
        self.p2s_map = None

        self.tri_l2g = tri_l2g
        self.node_l2g = node_l2g

        self.ghost_counter = 0

        # ---- exascale: timestep mode ----
        valid_modes = ('global', 'hierarchical', 'local')
        if timestep_mode not in valid_modes:
            raise ValueError(
                f"timestep_mode must be one of {valid_modes}, got {timestep_mode!r}")
        self.timestep_mode = timestep_mode
        self.local_timestep_safety_factor = float(local_timestep_safety_factor)

        if timestep_mode == 'hierarchical':
            generic_comms.setup_node_communicator(self)

        # ---- exascale: ghost exchange overlap ----
        self.overlap_ghost_exchange = bool(overlap_ghost_exchange)


    def set_boundary(self, boundary_map):
        """Set boundary conditions, automatically handling ghost edges.

        Parallel domains always have ghost edges (tagged 'ghost' in the
        mesh boundary dict).  Mesh.build_boundary_dictionary also adds
        'exterior' as the default tag for any boundary edge not explicitly
        classified during distribution.  This override injects both
        'ghost': None and 'exterior': None into the boundary_map so the
        user never needs to specify them explicitly.
        """
        parallel_internal_tags = {'ghost', 'exterior'}
        missing = parallel_internal_tags - set(boundary_map.keys())
        if missing:
            boundary_map = dict(boundary_map)
            for tag in missing:
                boundary_map[tag] = None
        Domain.set_boundary(self, boundary_map)


    def set_name(self, name):
        """Assign name based on processor number
        """

        if name.endswith('.sww'):
            name = name[:-4]

        self.global_name = name

        # Call parents method with processor number attached.
        Domain.set_name(self, name + '_P%d_%d' %(self.numproc, self.processor))


    def get_global_name(self):

        return self.global_name


    def update_timestep(self, yieldstep, finaltime):
        """Synchronise the CFL timestep across ranks using the configured mode.

        Dispatches to the function selected by ``self.timestep_mode``:
          * ``'global'``       – standard MPI_Allreduce(MIN) [default]
          * ``'hierarchical'`` – two-level intra+inter node reduction
          * ``'local'``        – no global sync; rank uses own local dt
        """
        if self.fixed_flux_timestep is None:
            mode = getattr(self, 'timestep_mode', 'global')
            if mode == 'hierarchical':
                generic_comms.communicate_flux_timestep_hierarchical(
                    self, yieldstep, finaltime)
            elif mode == 'local':
                generic_comms.communicate_flux_timestep_local(
                    self, yieldstep, finaltime)
            else:
                generic_comms.communicate_flux_timestep(
                    self, yieldstep, finaltime)

        Domain.update_timestep(self, yieldstep, finaltime)

    def update_ghosts(self, quantities=None):
        """Synchronise ghost-cell centroid values from neighbouring ranks.

        In overlap mode (``overlap_ghost_exchange=True``):
          * If a non-blocking exchange is pending (started at the end of the
            previous Euler step by ``evolve_one_euler_step``), wait for it to
            complete and copy the received data into centroid_values.
          * If no exchange is pending (e.g. the very first call before the
            evolve loop, or after an RK step), fall back to the standard
            blocking exchange so ghost values are always initialised.

        In non-overlap mode, always performs the standard blocking exchange.
        """
        # GPU mode handles ghost exchange internally via C-level MPI calls.
        if getattr(self, 'multiprocessor_mode', 0) == 2 and \
                getattr(self, 'gpu_interface', None) is not None:
            return

        if getattr(self, 'overlap_ghost_exchange', False):
            if getattr(self, '_ghost_exchange_pending', False):
                generic_comms.finish_ghost_exchange(self)
            else:
                # No pending exchange – perform a full blocking exchange.
                generic_comms.communicate_ghosts_non_blocking(self, quantities)
            return

        generic_comms.communicate_ghosts_non_blocking(self, quantities)

    def evolve_one_euler_step(self, yieldstep, finaltime):
        """One Euler time step with optional ghost-exchange overlap.

        When ``overlap_ghost_exchange=True``:
          1. Finish the non-blocking ghost exchange that was started at the end
             of the *previous* Euler step (or no-op on the first call).
          2. Run the standard Euler step (distribute → fluxes → update Q).
          3. Immediately post non-blocking Isend/Irecv for the new conserved
             quantities so the transfer runs concurrently with
             ``apply_fractional_steps()`` in the outer evolve loop.

        When ``overlap_ghost_exchange=False``, delegates to the parent
        implementation (no change in behaviour).

        Overlap is only active for ``'euler'`` timestepping.  RK2/RK3 use the
        standard ``update_ghosts`` path (blocking, called by ``_evolve_base``).
        """
        if not getattr(self, 'overlap_ghost_exchange', False):
            super().evolve_one_euler_step(yieldstep, finaltime)
            return

        try:
            from anuga.abstract_2d_finite_volumes.generic_domain import (
                nvtxRangePush, nvtxRangePop)
        except ImportError:
            def nvtxRangePush(_): pass
            def nvtxRangePop(): pass

        # Wait for the non-blocking ghost exchange from the previous step.
        generic_comms.finish_ghost_exchange(self)

        nvtxRangePush('distribute_to_vertices_and_edges')
        self.distribute_to_vertices_and_edges()
        nvtxRangePop()

        nvtxRangePush('update_boundary')
        self.update_boundary()
        nvtxRangePop()

        nvtxRangePush('compute_fluxes')
        self.compute_fluxes()
        nvtxRangePop()

        nvtxRangePush('compute_forcing_terms')
        self.compute_forcing_terms()
        nvtxRangePop()

        nvtxRangePush('update_timestep')
        self.update_timestep(yieldstep, finaltime)
        nvtxRangePop()

        nvtxRangePush('update_conserved_quantities')
        self.update_conserved_quantities()
        nvtxRangePop()

        # Start the ghost exchange for the next step.  The transfer runs
        # concurrently with apply_fractional_steps() in _evolve_base.
        generic_comms.start_ghost_exchange(self)

    def apply_fractional_steps(self):
        # Call parent implementation which handles GPU sync logic
        super().apply_fractional_steps()



    def sww_merge(self, verbose=False, delete_old=False, chunk_size=None):
        """Merge all the sub domain sww files into a global sww file

        :param bool verbose: Flag to produce more output
        :param bool delete_old: Flag to delete sub domain sww files after
            creating global sww file
        :param chunk_size: Maximum number of timesteps to hold in RAM at once
            during the merge.  ``None`` (default) reads all timesteps in a
            single pass (fastest).  Set to a positive integer (e.g. 100) to
            bound peak memory use when the full merged time series does not
            fit in RAM.
        :type chunk_size: int or None

        """

        # make sure all the computations have finished

        pypar.barrier()

        # now on processor 0 pull all the separate sww files together
        if self.processor == 0 and self.numproc > 1 and self.store :
            import anuga.utilities.sww_merge as merge

            global_name = join(self.get_datadir(),self.get_global_name())

            merge.sww_merge_parallel(global_name, self.numproc, verbose,
                                     delete_old, chunk_size=chunk_size)

        # make sure all the merge completes on processor 0 before other
        # processors complete (like when finalize is forgotten in main script)

        pypar.barrier()

    def write_time(self):

        if self.processor == 0:
            Domain.write_time(self)


    def load_balance_statistics(self, minimum_height=None):
        """Gather per-rank load balance statistics and return them on rank 0.

        Measures the correlation between wet fraction and computational load
        across MPI ranks.  The key insight is that dry triangles do much less
        work (flux computation is skipped via optimise_dry_cells), so a rank
        with many dry triangles finishes its compute phase early and then sits
        idle in the Allreduce timestep-sync barrier.  This idle time shows up
        in ``domain.communication_reduce_time``.

        Parameters
        ----------
        minimum_height : float, optional
            Depth threshold for "wet" classification.  Defaults to
            ``anuga.config.minimum_allowed_height``.

        Returns
        -------
        dict or None
            On rank 0: dictionary with arrays of length ``numproc``::

                n_full           int    owned (full) triangle count per rank
                n_ghost          int    ghost triangle count per rank
                n_wet_full       int    wet owned triangle count per rank
                wet_fraction     float  n_wet_full / n_full per rank
                ghost_fraction   float  n_ghost / (n_full + n_ghost) per rank
                wall_time        float  total wall time since evolve() started
                comm_time        float  ghost-exchange wall time per rank
                reduce_wait_time float  Allreduce wait time per rank (proxy for
                                        idle time: high ↔ low wet fraction)
                compute_time     float  wall_time - comm_time - reduce_wait_time
                imbalance_ratio  float  max(compute_time) / mean(compute_time)
                wet_compute_corr float  Pearson r(wet_fraction, compute_time)

            On all other ranks: ``None``.
        """
        from time import time as walltime
        from anuga.config import minimum_allowed_height as default_mah

        if minimum_height is None:
            minimum_height = default_mah

        # ---- per-rank statistics ----
        n_total    = len(self.tri_full_flag)
        full_mask  = (self.tri_full_flag == 1)
        n_full     = int(num.sum(full_mask))
        n_ghost    = n_total - n_full

        # wet count over FULL triangles only (owned work)
        stage_c = self.get_quantity('stage').centroid_values
        elev_c  = self.get_quantity('elevation').centroid_values
        depth_c = stage_c - elev_c
        n_wet_full = int(num.sum((depth_c > minimum_height) & full_mask))

        w_time  = walltime() - self.evolve_start_walltime
        c_time  = self.communication_time
        r_time  = self.communication_reduce_time

        # Pack into a single float array for Allgather
        local_stats = num.array([
            float(n_full),
            float(n_ghost),
            float(n_wet_full),
            w_time,
            c_time,
            r_time,
        ], dtype=float)

        n_fields = len(local_stats)
        all_stats = num.zeros(self.numproc * n_fields, dtype=float)

        from mpi4py.MPI import DOUBLE
        pypar.comm.Allgather(
            [local_stats, DOUBLE],
            [all_stats,   DOUBLE],
        )

        if self.processor != 0:
            return None

        # ---- unpack on rank 0 ----
        s = all_stats.reshape(self.numproc, n_fields)
        n_full_arr   = s[:, 0].astype(int)
        n_ghost_arr  = s[:, 1].astype(int)
        n_wet_arr    = s[:, 2].astype(int)
        wall_arr     = s[:, 3]
        comm_arr     = s[:, 4]
        reduce_arr   = s[:, 5]

        wet_frac     = num.where(n_full_arr > 0,
                                 n_wet_arr / n_full_arr, 0.0)
        ghost_frac   = (n_ghost_arr /
                        num.maximum(n_full_arr + n_ghost_arr, 1).astype(float))
        compute_arr  = num.maximum(wall_arr - comm_arr - reduce_arr, 0.0)

        imbalance = (float(num.max(compute_arr)) / float(num.mean(compute_arr))
                     if num.mean(compute_arr) > 0 else 1.0)

        # Pearson correlation between wet_fraction and compute_time
        if self.numproc > 1 and num.std(wet_frac) > 0 and num.std(compute_arr) > 0:
            corr = float(num.corrcoef(wet_frac, compute_arr)[0, 1])
        else:
            corr = float('nan')

        return {
            'n_full':           n_full_arr,
            'n_ghost':          n_ghost_arr,
            'n_wet_full':       n_wet_arr,
            'wet_fraction':     wet_frac,
            'ghost_fraction':   ghost_frac,
            'wall_time':        wall_arr,
            'comm_time':        comm_arr,
            'reduce_wait_time': reduce_arr,
            'compute_time':     compute_arr,
            'imbalance_ratio':  imbalance,
            'wet_compute_corr': corr,
        }

    def print_load_balance_statistics(self, minimum_height=None):
        """Print a formatted load balance report to stdout (rank 0 only).

        Calls :meth:`load_balance_statistics` and formats the result as a
        table with one row per MPI rank.  Also prints the overall imbalance
        ratio and the Pearson correlation between wet fraction and compute time.

        Parameters
        ----------
        minimum_height : float, optional
            Passed through to :meth:`load_balance_statistics`.
        """
        stats = self.load_balance_statistics(minimum_height=minimum_height)
        if stats is None:
            return  # only rank 0 prints

        np = self.numproc
        header = (
            f"\n{'Load balance statistics':=<72}\n"
            f"{'Rank':>4}  {'n_full':>8}  {'n_ghost':>8}  "
            f"{'wet%':>6}  {'ghost%':>6}  "
            f"{'compute(s)':>10}  {'comm(s)':>8}  {'Allreduce_wait(s)':>17}\n"
            f"{'-'*72}"
        )
        print(header)
        for i in range(np):
            print(
                f"{i:>4}  {stats['n_full'][i]:>8d}  {stats['n_ghost'][i]:>8d}  "
                f"{100*stats['wet_fraction'][i]:>6.1f}  "
                f"{100*stats['ghost_fraction'][i]:>6.1f}  "
                f"{stats['compute_time'][i]:>10.3f}  "
                f"{stats['comm_time'][i]:>8.3f}  "
                f"{stats['reduce_wait_time'][i]:>17.3f}"
            )
        print(f"{'='*72}")
        print(f"  Imbalance ratio (max/mean compute): {stats['imbalance_ratio']:.3f}")
        corr = stats['wet_compute_corr']
        if corr == corr:  # not NaN
            print(f"  Pearson r(wet_fraction, compute_time): {corr:+.3f}")
            if abs(corr) > 0.5:
                direction = 'wetter ranks do more work' if corr > 0 else 'unexpected'
                print(f"  Interpretation: {direction} (|r| = {abs(corr):.3f})")
        print()

    def dump_triangulation(self, filename="domain.png"):
        """
        Outputs domain triangulation, full triangles are shown in green while ghost triangles are shown in blue.
        The default filename is 'domain.png'
        """

        # Get vertex coordinates, partition full and ghost triangles based on self.tri_full_flag

        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            import matplotlib.tri as tri
        except ImportError:
            print("Couldn't import module from matplotlib, probably you need to update matplotlib")
            raise

        vertices = self.get_vertex_coordinates()
        full_mask = num.repeat(self.tri_full_flag == 1, 3)
        ghost_mask = num.repeat(self.tri_full_flag == 0, 3)

        myid = pypar.rank()
        numprocs = pypar.size()

        if myid == 0:

            fig = plt.figure()
            fx = {}
            fy = {}
            gx = {}
            gy = {}

            # Proc 0 gathers full and ghost nodes from self and other processors
            fx[0] = vertices[full_mask,0]
            fy[0] = vertices[full_mask,1]
            gx[0] = vertices[ghost_mask,0]
            gy[0] = vertices[ghost_mask,1]

            for i in range(1,numprocs):
                fx[i] = pypar.receive(i)
                fy[i] = pypar.receive(i)
                gx[i] = pypar.receive(i)
                gy[i] = pypar.receive(i)

            # Plot full triangles
            for i in range(0, numprocs):
                n = int(len(fx[i])//3)

                triang = num.array(list(range(0,3*n)))
                triang.shape = (n, 3)
                plt.triplot(fx[i], fy[i], triang, 'g-', linewidth = 0.5)

            # Plot ghost triangles
            for i in range(0, numprocs):
                n = int(len(gx[i])//3)
                if n > 0:
                    triang = num.array(list(range(0,3*n)))
                    triang.shape = (n, 3)
                    plt.triplot(gx[i], gy[i], triang, 'b--', linewidth = 0.5)

            # Save triangulation to location pointed by filename
            plt.savefig(filename, dpi=600)

        else:
            # Proc 1..numprocs send full and ghost triangles to Proc 0
            pypar.send(vertices[full_mask,0], 0)
            pypar.send(vertices[full_mask,1], 0)
            pypar.send(vertices[ghost_mask,0], 0)
            pypar.send(vertices[ghost_mask,1], 0)


    def dump_local_triangulation(self, filename=None):
        '''
        Outputs domain triangulation, full triangles are shown in green while
        ghost triangles are shown in blue.

        The default filename is self.get_name()+'.png'
        '''

        if filename is None:
            filename = self.get_name() + '.png'

        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            import matplotlib.tri as tri
        except ImportError:
            print("Couldn't import module from matplotlib, probably you need to update matplotlib")
            raise

        vertices = self.get_vertex_coordinates()
        full_mask = num.repeat(self.tri_full_flag == 1, 3)
        ghost_mask = num.repeat(self.tri_full_flag == 0, 3)

        plt.figure()

        fx = vertices[full_mask,0]
        fy = vertices[full_mask,1]
        gx = vertices[ghost_mask,0]
        gy = vertices[ghost_mask,1]

        # Plot full triangles
        n = int(len(fx)/3)
        triang = num.array(list(range(0,3*n)))
        triang.shape = (n, 3)
        plt.triplot(fx, fy, triang, 'g-', linewidth = 0.5)

        # Plot ghost triangles
        n = int(len(gx)/3)
        if n > 0:
            triang = num.array(list(range(0,3*n)))
            triang.shape = (n, 3)
            plt.triplot(gx, gy, triang, 'b--', linewidth = 0.5)

        # Save triangulation to location pointed by filename
        plt.savefig(filename, dpi=600)
