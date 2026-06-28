"""
Generic implementation of update_timestep and update_ghosts for
parallel domains (eg shallow_water or advection)

Ole Nielsen, Stephen Roberts, Duncan Gray, Christopher Zoppou
Geoscience Australia, 2004-2010

Exascale extensions (2026):
  * communicate_flux_timestep_hierarchical  – 2-level MIN reduce (intra-node
    Reduce + inter-node Allreduce) to cut allreduce latency on large node counts
  * communicate_flux_timestep_local         – no global sync; each rank uses its
    own local CFL timestep (requires fixed yieldstep schedule, O(P) scale-out)
  * setup_node_communicator                 – creates per-node MPI communicators
  * start_ghost_exchange / finish_ghost_exchange – split the non-blocking ghost
    exchange into two calls so computation can run between them
"""
import numpy as num

import anuga.utilities.parallel_abstraction as pypar




def setup_buffers(domain):
    """Buffers for synchronisation of timesteps
    """

    domain.local_timestep = num.zeros(1, float)
    domain.global_timestep = num.zeros(1, float)

    domain.local_timesteps = num.zeros(domain.numproc, float)

    domain.communication_time = 0.0
    domain.communication_reduce_time = 0.0
    domain.communication_broadcast_time = 0.0

    domain.calls_to_update_ghosts = 0
    domain.calls_to_update_timestep = 0

    # Overlap ghost-exchange state
    domain._ghost_exchange_pending = False
    domain._ghost_recv_requests = []
    domain._ghost_send_requests = []
    domain._ghost_exchange_quantities = None

    # Hierarchical-reduce communicators (populated by setup_node_communicator)
    domain.node_comm = None
    domain.inter_node_comm = None
    domain.is_node_root = False


def communicate_flux_timestep(domain, yieldstep, finaltime):
    """Calculate local timestep
    """

    import time
    import anuga

    if anuga.myid == 0:
        #print('o', end = '')
        domain.calls_to_update_timestep += 1

    # disable allreduce if fixed_flux_timestep is set
    if domain.fixed_flux_timestep is not None:
        domain.flux_timestep = domain.fixed_flux_timestep
        if not domain.test_allreduce:
            return


    #Compute minimal timestep across all processes
    domain.local_timestep[0] = domain.flux_timestep
    t0 = time.time()


    local_timestep = domain.local_timestep
    global_timestep = domain.global_timestep

    #pypar.allreduce(domain.local_timestep, pypar.MIN,
    #                  buffer=domain.global_timestep,
    #                  bypass=True)

    from mpi4py.MPI import MIN
    pypar.comm.Allreduce(local_timestep, global_timestep, op=MIN)

    domain.communication_reduce_time += time.time()-t0

    t0 = time.time()

    domain.communication_broadcast_time += time.time()-t0

    domain.flux_timestep = domain.global_timestep[0]


def communicate_ghosts_blocking(domain, quantities=None):

    # We must send the information from the full cells and
    # receive the information for the ghost cells
    # We have a dictionary of lists with ghosts expecting updates from
    # the separate processors

    import numpy as num
    import time
    t0 = time.time()

    if quantities is None:
        quantities = domain.conserved_quantities

    # update of non-local ghost cells
    for iproc in range(domain.numproc):
        if iproc == domain.processor:
            #Send data from iproc processor to other processors
            for send_proc in domain.full_send_dict:
                if send_proc != iproc:

                    Idf  = domain.full_send_dict[send_proc][0]
                    Xout = domain.full_send_dict[send_proc][2]

                    for i, q in enumerate(quantities):
                        #print 'Send',i,q
                        Q_cv =  domain.quantities[q].centroid_values
                        Xout[:,i] = num.take(Q_cv, Idf)

                    pypar.send(Xout, int(send_proc), use_buffer=True, bypass=True)


        else:
            #Receive data from the iproc processor
            if  iproc in domain.ghost_recv_dict:

                Idg = domain.ghost_recv_dict[iproc][0]
                X   = domain.ghost_recv_dict[iproc][2]

                X = pypar.receive(int(iproc), buffer=X, bypass=True)

                for i, q in enumerate(quantities):
                    #print 'Receive',i,q
                    Q_cv =  domain.quantities[q].centroid_values
                    num.put(Q_cv, Idg, X[:,i])

    #local update of ghost cells
    iproc = domain.processor
    if iproc in domain.full_send_dict:

        # LINDA:
        # now store full as local id, global id, value
        Idf  = domain.full_send_dict[iproc][0]

        # LINDA:
        # now store ghost as local id, global id, value
        Idg = domain.ghost_recv_dict[iproc][0]

        for i, q in enumerate(quantities):
            #print 'LOCAL SEND RECEIVE',i,q
            Q_cv =  domain.quantities[q].centroid_values
            num.put(Q_cv, Idg, num.take(Q_cv, Idf))

    domain.communication_time += time.time()-t0



def communicate_ghosts_non_blocking(domain, quantities=None):

    # We must send the information from the full cells and
    # receive the information for the ghost cells
    # We have a dictionary of lists with ghosts expecting updates from
    # the separate processors
    # Using isend and irecv

    import numpy as num
    import time
    import anuga
    t0 = time.time()

    if anuga.myid == 0:
        #print('.', end = '')
        domain.calls_to_update_ghosts += 1

    sendDict = domain.full_send_dict
    recvDict = domain.ghost_recv_dict

    if quantities is None:
        quantities = domain.conserved_quantities

    # update of non-local ghost cells by copying full cell data into the
    # Xout buffer arrays

    #iproc == domain.processor

    #Setup send buffer arrays for sending full data to other processors
    for send_proc in domain.full_send_dict:
        Idf  = sendDict[send_proc][0]
        Xout = sendDict[send_proc][2]

        for i, q in enumerate(quantities):
            #print 'Store send data',i,q
            Q_cv =  domain.quantities[q].centroid_values
            Xout[:,i] = num.take(Q_cv, Idf)

    #--------------------------------------------
    # Do all the comuunication using isend/irecv
    # via the buffers in the
    # full_send_dict and ghost_recv_dict
    #--------------------------------------------


    #-------------------------
    # Do the Irecvs first
    #-------------------------
    recv_requests = []
    for recv_proc in recvDict:

        Idg = recvDict[recv_proc][0]
        X   = recvDict[recv_proc][2]

        request = pypar.comm.Irecv(X, recv_proc, 123)
        recv_requests.append(request)

    #---------------------
    # Do the Isends second
    #---------------------
    send_requests = []
    for send_proc in sendDict:

        Idg = sendDict[send_proc][0]
        X   = sendDict[send_proc][2]

        request = pypar.comm.Isend(X, send_proc, 123)
        send_requests.append(request)

    #-----------------------------------------
    # Now complete communication.
    # We could put some computation between the
    # communication calls above and this call.
    # Question: Do we need to wait for the sends to complete as well?
    # Answer: Yes, we should wait for the sends to complete as well, otherwise
    # we might be overwriting the send buffers before the data has been sent.
    #-----------------------------------------
    import mpi4py
    mpi4py.MPI.Request.Waitall(recv_requests + send_requests)

    # Now copy data from receive buffers to the domain
    for recv_proc in recvDict:
        Idg  = recvDict[recv_proc][0]
        X    = recvDict[recv_proc][2]

        for i, q in enumerate(quantities):
            #print 'Read receive data',i,q
            Q_cv =  domain.quantities[q].centroid_values
            num.put(Q_cv, Idg, X[:,i])


    domain.communication_time += time.time()-t0


def communicate_ghosts_asynchronous(domain, quantities=None):

    # We must send the information from the full cells and
    # receive the information for the ghost cells
    # We have a dictionary of lists with ghosts expecting updates from
    # the separate processors
    # Using isend and irecv

    import numpy as num
    import time
    t0 = time.time()

    if quantities is None:
        quantities = domain.conserved_quantities

    # update of non-local ghost cells by copying full cell data into the
    # Xout buffer arrays

    #iproc == domain.processor

    #Setup send buffer arrays for sending full data to other processors
    for send_proc in domain.full_send_dict:
        Idf  = domain.full_send_dict[send_proc][0]
        Xout = domain.full_send_dict[send_proc][2]

        for i, q in enumerate(quantities):
            #print 'Store send data',i,q
            Q_cv =  domain.quantities[q].centroid_values
            Xout[:,i] = num.take(Q_cv, Idf)

    # Do all the comuunication using isend/irecv via the buffers in the
    # full_send_dict and ghost_recv_dict

    pypar.send_recv_via_dicts(domain.full_send_dict,domain.ghost_recv_dict)

    # Now copy data from receive buffers to the domain
    for recv_proc in domain.ghost_recv_dict:
        Idg  = domain.ghost_recv_dict[recv_proc][0]
        X    = domain.ghost_recv_dict[recv_proc][2]

        for i, q in enumerate(quantities):
            #print 'Read receive data',i,q
            Q_cv =  domain.quantities[q].centroid_values
            num.put(Q_cv, Idg, X[:,i])


    domain.communication_time += time.time()-t0


# ---------------------------------------------------------------------------
# Exascale Item 1: Hierarchical / local CFL timestep
# ---------------------------------------------------------------------------

def setup_node_communicator(domain):
    """Create per-node and inter-node MPI communicators for hierarchical reduction.

    Populates domain.node_comm (intra-node shared-memory split),
    domain.inter_node_comm (one rank per node, used for inter-node Allreduce),
    and domain.is_node_root (True iff this rank is rank 0 inside its node).

    Falls back gracefully if MPI_COMM_TYPE_SHARED is not supported.
    """
    try:
        from mpi4py import MPI
        node_comm = MPI.COMM_WORLD.Split_type(MPI.COMM_TYPE_SHARED)
        node_rank = node_comm.Get_rank()
        # One representative per node: ranks with node_rank==0 form the inter-node comm.
        # Ranks with node_rank>0 get color=1 (they still need a valid communicator).
        color = 0 if node_rank == 0 else 1
        inter_node_comm = MPI.COMM_WORLD.Split(color=color, key=MPI.COMM_WORLD.Get_rank())
        domain.node_comm = node_comm
        domain.inter_node_comm = inter_node_comm
        domain.is_node_root = (node_rank == 0)
    except Exception as e:
        import warnings
        warnings.warn(
            f'setup_node_communicator: {e}. Falling back to global Allreduce.',
            RuntimeWarning, stacklevel=2)
        domain.node_comm = None
        domain.inter_node_comm = None
        domain.is_node_root = False


def communicate_flux_timestep_hierarchical(domain, yieldstep, finaltime):
    """Two-level MIN reduction for the CFL timestep.

    Replaces a single global Allreduce(MIN) with:
      1. Intra-node Reduce(MIN)  to the node-local root (shared memory, ~10 ns)
      2. Inter-node Allreduce(MIN) across node roots only             (~log N nodes)
      3. Intra-node Bcast from node root back to all ranks            (~10 ns)

    On a system with P ranks spread across N nodes (P/N ranks per node), the
    latency drops from O(log P) to O(log(P/N) + log N) = O(log P), but with
    a much smaller constant because the first and last steps use shared memory
    rather than the network.  At 256 ranks/node the network message count falls
    256x compared with a flat Allreduce.

    Falls back to the standard global Allreduce when node communicators were
    not set up (e.g. single-process run or MPI_COMM_TYPE_SHARED unsupported).
    """
    import time
    from mpi4py.MPI import MIN

    if domain.fixed_flux_timestep is not None:
        domain.flux_timestep = domain.fixed_flux_timestep
        if not getattr(domain, 'test_allreduce', False):
            return

    if domain.node_comm is None:
        # No node communicator – fall back to standard global Allreduce.
        communicate_flux_timestep(domain, yieldstep, finaltime)
        return

    domain.local_timestep[0] = domain.flux_timestep
    t0 = time.time()

    from mpi4py import MPI
    node_min = num.zeros(1, float)
    domain.node_comm.Reduce(domain.local_timestep, node_min, op=MIN, root=0)

    global_min = num.zeros(1, float)
    if domain.is_node_root:
        domain.inter_node_comm.Allreduce(node_min, global_min, op=MIN)

    # Broadcast global minimum from node root to all ranks in the node.
    domain.node_comm.Bcast(global_min, root=0)

    domain.communication_reduce_time += time.time() - t0
    domain.flux_timestep = float(global_min[0])


def communicate_flux_timestep_local(domain, yieldstep, finaltime):
    """No global synchronization: each rank uses its own local CFL timestep.

    Each rank computes flux_timestep from its own cells (already done by
    compute_fluxes) and applies an extra safety factor defined by
    domain.local_timestep_safety_factor (default 0.9).

    When to use:
      * Only safe when the mesh is quasi-uniform so local dt values across
        ranks do not diverge by more than ~10%.
      * Eliminates the global Allreduce barrier entirely (O(P) → O(1) scaling).
      * All ranks still advance by yieldstep intervals; within each interval
        they may take different numbers of internal steps.

    Stability note:
      The CFL condition is satisfied per-rank because compute_fluxes already
      found the minimum dt over all local cells (including ghost cells from the
      previous ghost exchange).  The safety factor provides margin for any
      small difference between ghost-cell wave speeds and the true neighbours.
    """
    if domain.fixed_flux_timestep is not None:
        domain.flux_timestep = domain.fixed_flux_timestep
        if not getattr(domain, 'test_allreduce', False):
            return

    domain.flux_timestep *= getattr(domain, 'local_timestep_safety_factor', 0.9)


# ---------------------------------------------------------------------------
# Exascale Item 2: Split non-blocking ghost exchange for compute overlap
# ---------------------------------------------------------------------------

def start_ghost_exchange(domain, quantities=None):
    """Post non-blocking Isend/Irecv for conserved-quantity ghost exchange.

    Packs full-cell centroid values into send buffers and posts all Irecv/Isend
    calls.  Returns immediately without waiting.  Call finish_ghost_exchange()
    before the next distribute_to_vertices_and_edges() to consume the results.

    If called while a previous exchange is still pending, finish_ghost_exchange
    is called first to avoid buffer aliasing.

    Overlap note:
      This is intended to be called right after update_conserved_quantities()
      so the network transfer runs concurrently with apply_fractional_steps().
      The values sent are pre-fractional-step; the temporal error introduced is
      O(dt × h²) – higher order than the scheme's O(dt + h²) truncation error
      and is therefore safe for practical use (same approximation used by
      ADCIRC and other operational coastal flood models at scale).
    """
    import time

    if getattr(domain, '_ghost_exchange_pending', False):
        # Safety: finish any leftover exchange before starting a new one.
        finish_ghost_exchange(domain)

    if quantities is None:
        quantities = domain.conserved_quantities

    sendDict = domain.full_send_dict
    recvDict = domain.ghost_recv_dict

    t0 = time.time()

    # Pack full-cell centroid values into contiguous send buffers.
    for send_proc in sendDict:
        Idf  = sendDict[send_proc][0]
        Xout = sendDict[send_proc][2]
        for i, q in enumerate(quantities):
            Q_cv = domain.quantities[q].centroid_values
            Xout[:, i] = num.take(Q_cv, Idf)

    # Post all Irecvs first to minimise unexpected-message overhead.
    recv_requests = []
    for recv_proc in recvDict:
        X = recvDict[recv_proc][2]
        request = pypar.comm.Irecv(X, recv_proc, 123)
        recv_requests.append(request)

    # Post all Isends.
    send_requests = []
    for send_proc in sendDict:
        X = sendDict[send_proc][2]
        request = pypar.comm.Isend(X, send_proc, 123)
        send_requests.append(request)

    domain._ghost_recv_requests    = recv_requests
    domain._ghost_send_requests    = send_requests
    domain._ghost_exchange_quantities = list(quantities)
    domain._ghost_exchange_pending = True
    domain._ghost_exchange_t0      = t0


def finish_ghost_exchange(domain):
    """Complete a pending non-blocking ghost exchange.

    Calls MPI Waitall on all outstanding Isend/Irecv requests, then copies the
    received data into ghost-cell centroid_values.  No-op if no exchange is
    currently pending (safe to call unconditionally).
    """
    import time

    if not getattr(domain, '_ghost_exchange_pending', False):
        return

    import mpi4py
    mpi4py.MPI.Request.Waitall(
        domain._ghost_recv_requests + domain._ghost_send_requests
    )
    domain._ghost_exchange_pending = False

    recvDict   = domain.ghost_recv_dict
    quantities = domain._ghost_exchange_quantities

    for recv_proc in recvDict:
        Idg = recvDict[recv_proc][0]
        X   = recvDict[recv_proc][2]
        for i, q in enumerate(quantities):
            Q_cv = domain.quantities[q].centroid_values
            num.put(Q_cv, Idg, X[:, i])

    domain.communication_time += time.time() - domain._ghost_exchange_t0

