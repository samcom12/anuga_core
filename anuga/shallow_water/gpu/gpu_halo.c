// GPU-accelerated shallow water solver
// Split from sw_domain_gpu.c for maintainability

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <omp.h>
#include <mpi.h>
#include "gpu_domain.h"
#include "gpu_omp_macros.h"
#include "gpu_nvtx.h"

// Halo exchange setup and MPI ghost exchange

// ============================================================================
// Halo Exchange Setup
// ============================================================================

int gpu_halo_init(struct gpu_domain *GD,
                  int num_neighbors,
                  int *neighbor_ranks,
                  int *send_counts,
                  int *recv_counts,
                  int *flat_send_indices,
                  int *flat_recv_indices) {
    struct halo_exchange *H = &GD->halo;

    H->num_neighbors = num_neighbors;

    if (num_neighbors == 0) {
        // No communication needed
        return 0;
    }

    // Allocate and copy neighbor info
    H->neighbor_ranks = (int *)malloc(num_neighbors * sizeof(int));
    H->send_counts = (int *)malloc(num_neighbors * sizeof(int));
    H->recv_counts = (int *)malloc(num_neighbors * sizeof(int));
    H->send_offsets = (int *)malloc((num_neighbors + 1) * sizeof(int));
    H->recv_offsets = (int *)malloc((num_neighbors + 1) * sizeof(int));

    memcpy(H->neighbor_ranks, neighbor_ranks, num_neighbors * sizeof(int));
    memcpy(H->send_counts, send_counts, num_neighbors * sizeof(int));
    memcpy(H->recv_counts, recv_counts, num_neighbors * sizeof(int));

    // Compute total sizes and offsets
    H->total_send_size = 0;
    H->total_recv_size = 0;
    H->send_offsets[0] = 0;
    H->recv_offsets[0] = 0;

    for (int ni = 0; ni < num_neighbors; ni++) {
        H->total_send_size += send_counts[ni];
        H->total_recv_size += recv_counts[ni];
        H->send_offsets[ni + 1] = H->total_send_size;
        H->recv_offsets[ni + 1] = H->total_recv_size;
    }

    // Allocate and copy flattened index arrays
    H->flat_send_indices = (int *)malloc(H->total_send_size * sizeof(int));
    H->flat_recv_indices = (int *)malloc(H->total_recv_size * sizeof(int));

    memcpy(H->flat_send_indices, flat_send_indices, H->total_send_size * sizeof(int));
    memcpy(H->flat_recv_indices, flat_recv_indices, H->total_recv_size * sizeof(int));

    // Allocate communication buffers
    // 3 quantities per element: stage, xmom, ymom centroid values
#ifdef GPU_AWARE_MPI
    if (GD->device_id >= 0) {
        // Device buffers for GPU pack/unpack kernels
        int dev = omp_get_default_device();
        H->send_buffer = (double *)omp_target_alloc(3 * H->total_send_size * sizeof(double), dev);
        H->recv_buffer = (double *)omp_target_alloc(3 * H->total_recv_size * sizeof(double), dev);
        if (!H->send_buffer || !H->recv_buffer) {
            fprintf(stderr, "ERROR: omp_target_alloc failed for halo buffers\n");
            return -1;
        }
        // Host staging buffers for MPI calls.
        // Some UCX transports (e.g. uct_mm shared-memory used intra-node) cannot
        // access omp_target_alloc device pointers, causing a SIGSEGV in MPI_Isend.
        // We always stage through host memory; the overhead is small because halos
        // are tiny compared to the full domain.
        H->host_send_buffer = (double *)malloc(3 * H->total_send_size * sizeof(double));
        H->host_recv_buffer = (double *)malloc(3 * H->total_recv_size * sizeof(double));
        if (!H->host_send_buffer || !H->host_recv_buffer) {
            fprintf(stderr, "ERROR: malloc failed for halo host staging buffers\n");
            return -1;
        }
    } else {
        // No GPU: fall back to host malloc even in a GPU_AWARE_MPI build
        H->send_buffer = (double *)malloc(3 * H->total_send_size * sizeof(double));
        H->recv_buffer = (double *)malloc(3 * H->total_recv_size * sizeof(double));
        H->host_send_buffer = NULL;
        H->host_recv_buffer = NULL;
    }
#else
    H->send_buffer = (double *)malloc(3 * H->total_send_size * sizeof(double));
    H->recv_buffer = (double *)malloc(3 * H->total_recv_size * sizeof(double));
    H->host_send_buffer = NULL;
    H->host_recv_buffer = NULL;
#endif

    // Allocate MPI request array
    H->requests = (MPI_Request *)malloc(2 * num_neighbors * sizeof(MPI_Request));

    if (GD->rank == 0) {
        printf("GPU halo exchange initialized:\n");
        printf("  Neighbors: %d\n", num_neighbors);
        printf("  Total send: %d elements\n", H->total_send_size);
        printf("  Total recv: %d elements\n", H->total_recv_size);
    }

    return 0;
}

void gpu_halo_finalize(struct gpu_domain *GD) {
    struct halo_exchange *H = &GD->halo;

    if (H->neighbor_ranks) free(H->neighbor_ranks);
    if (H->send_counts) free(H->send_counts);
    if (H->recv_counts) free(H->recv_counts);
    if (H->send_offsets) free(H->send_offsets);
    if (H->recv_offsets) free(H->recv_offsets);
    if (H->flat_send_indices) free(H->flat_send_indices);
    if (H->flat_recv_indices) free(H->flat_recv_indices);
#ifdef GPU_AWARE_MPI
    if (GD->device_id >= 0) {
        int dev = omp_get_default_device();
        if (H->send_buffer) omp_target_free(H->send_buffer, dev);
        if (H->recv_buffer) omp_target_free(H->recv_buffer, dev);
        if (H->host_send_buffer) free(H->host_send_buffer);
        if (H->host_recv_buffer) free(H->host_recv_buffer);
    } else {
        if (H->send_buffer) free(H->send_buffer);
        if (H->recv_buffer) free(H->recv_buffer);
    }
#else
    if (H->send_buffer) free(H->send_buffer);
    if (H->recv_buffer) free(H->recv_buffer);
#endif
    if (H->requests) free(H->requests);

    H->num_neighbors = 0;
    H->neighbor_ranks = NULL;
    H->send_counts = NULL;
    H->recv_counts = NULL;
    H->send_offsets = NULL;
    H->recv_offsets = NULL;
    H->flat_send_indices = NULL;
    H->flat_recv_indices = NULL;
    H->send_buffer = NULL;
    H->recv_buffer = NULL;
    H->host_send_buffer = NULL;
    H->host_recv_buffer = NULL;
    H->requests = NULL;
}


// ============================================================================
// Ghost Exchange - Split into begin/end for computation-communication overlap
// ============================================================================

// gpu_exchange_ghosts_begin: pack halo data on GPU, post MPI_Irecv BEFORE
// the device-to-host copy, then post MPI_Isend once data is on the host.
//
// By posting MPI_Irecv before the D2H transfer, the MPI library can begin
// registering the receive buffer with the network hardware while the PCIe
// bus is occupied by the D2H copy — hiding part of the MPI latency.
//
// gpu_exchange_ghosts_end must be called after any work that can safely run
// while the MPI transfer is in flight (e.g. gpu_protect on local cells,
// which does not read ghost cell values).
//
// Request layout in H->requests[0 .. 2*num_neighbors-1]:
//   [0 .. num_neighbors-1]        : MPI_Irecv requests (posted in begin)
//   [num_neighbors .. 2*nn-1]     : MPI_Isend requests (posted in begin)
// gpu_exchange_ghosts_end calls MPI_Waitall(2*num_neighbors, ...).

void gpu_exchange_ghosts_begin(struct gpu_domain *GD) {
    NVTX_PUSH("gpu_exchange_ghosts_begin");
    struct halo_exchange *H = &GD->halo;

    if (H->num_neighbors == 0) {
        NVTX_POP();
        return;
    }

    int send_size = H->total_send_size;

    double *stage = GD->D.stage_centroid_values;
    double *xmom = GD->D.xmom_centroid_values;
    double *ymom = GD->D.ymom_centroid_values;
    double *send_buf = H->send_buffer;
    double *recv_buf = H->recv_buffer;
    int *flat_send = H->flat_send_indices;

    // --- Pack halo send buffer on GPU ---
#ifdef GPU_AWARE_MPI
    if (GD->device_id >= 0) {
        #pragma omp target teams distribute parallel for is_device_ptr(send_buf)
        for (int idx = 0; idx < send_size; idx++) {
            int k = flat_send[idx];
            send_buf[3*idx + 0] = stage[k];
            send_buf[3*idx + 1] = xmom[k];
            send_buf[3*idx + 2] = ymom[k];
        }
    } else {
        for (int idx = 0; idx < send_size; idx++) {
            int k = flat_send[idx];
            send_buf[3*idx + 0] = stage[k];
            send_buf[3*idx + 1] = xmom[k];
            send_buf[3*idx + 2] = ymom[k];
        }
    }
#else
    OMP_PARALLEL_LOOP
    for (int idx = 0; idx < send_size; idx++) {
        int k = flat_send[idx];
        send_buf[3*idx + 0] = stage[k];
        send_buf[3*idx + 1] = xmom[k];
        send_buf[3*idx + 2] = ymom[k];
    }
#endif

    // --- Post Irecv / D2H / Isend ---
#ifdef GPU_AWARE_MPI
    if (GD->device_id >= 0) {
        double *host_send = H->host_send_buffer;
        double *host_recv = H->host_recv_buffer;
        int host = omp_get_initial_device();
        int dev  = omp_get_default_device();

        // Post all receives BEFORE D2H: the network receive path can be set up
        // while the PCIe D2H transfer runs, reducing total exchange latency.
        int recv_offset = 0;
        for (int ni = 0; ni < H->num_neighbors; ni++) {
            int partner = H->neighbor_ranks[ni];
            int count = H->recv_counts[ni];
            MPI_Irecv(&host_recv[3*recv_offset], 3*count, MPI_DOUBLE,
                      partner, 0, GD->comm, &H->requests[ni]);
            recv_offset += count;
        }

        // D2H copy of packed send buffer (overlaps with MPI receive registration)
        omp_target_memcpy(host_send, send_buf,
                          3 * send_size * sizeof(double),
                          0, 0, host, dev);

        // Post all sends now that host_send is ready
        int send_offset = 0;
        for (int ni = 0; ni < H->num_neighbors; ni++) {
            int partner = H->neighbor_ranks[ni];
            int count = H->send_counts[ni];
            MPI_Isend(&host_send[3*send_offset], 3*count, MPI_DOUBLE,
                      partner, 0, GD->comm, &H->requests[H->num_neighbors + ni]);
            send_offset += count;
        }
    } else {
        // CPU-only: buffers are host malloc — post Irecv then Isend directly
        int recv_offset = 0;
        for (int ni = 0; ni < H->num_neighbors; ni++) {
            int partner = H->neighbor_ranks[ni];
            int count = H->recv_counts[ni];
            MPI_Irecv(&recv_buf[3*recv_offset], 3*count, MPI_DOUBLE,
                      partner, 0, GD->comm, &H->requests[ni]);
            recv_offset += count;
        }
        int send_offset = 0;
        for (int ni = 0; ni < H->num_neighbors; ni++) {
            int partner = H->neighbor_ranks[ni];
            int count = H->send_counts[ni];
            MPI_Isend(&send_buf[3*send_offset], 3*count, MPI_DOUBLE,
                      partner, 0, GD->comm, &H->requests[H->num_neighbors + ni]);
            send_offset += count;
        }
    }
#else
    // Non-GPU-aware MPI: post Irecv BEFORE D2H to allow the MPI library to
    // start registering the receive buffer while the PCIe transfer is in flight.
    int recv_offset = 0;
    for (int ni = 0; ni < H->num_neighbors; ni++) {
        int partner = H->neighbor_ranks[ni];
        int count = H->recv_counts[ni];
        MPI_Irecv(&recv_buf[3*recv_offset], 3*count, MPI_DOUBLE,
                  partner, 0, GD->comm, &H->requests[ni]);
        recv_offset += count;
    }

    // D2H copy of packed send buffer (overlaps with MPI receive registration)
    if (GD->device_id >= 0) {
        #pragma omp target update from(send_buf[0:3*send_size])
    }

    // Post all sends now that host-side send_buf is populated
    int send_offset = 0;
    for (int ni = 0; ni < H->num_neighbors; ni++) {
        int partner = H->neighbor_ranks[ni];
        int count = H->send_counts[ni];
        MPI_Isend(&send_buf[3*send_offset], 3*count, MPI_DOUBLE,
                  partner, 0, GD->comm, &H->requests[H->num_neighbors + ni]);
        send_offset += count;
    }
#endif
    NVTX_POP();
}

// gpu_exchange_ghosts_end: wait for all in-flight MPI requests posted by
// gpu_exchange_ghosts_begin, copy received data host→device, unpack on GPU.
void gpu_exchange_ghosts_end(struct gpu_domain *GD) {
    NVTX_PUSH("gpu_exchange_ghosts_end");
    struct halo_exchange *H = &GD->halo;

    if (H->num_neighbors == 0) {
        NVTX_POP();
        return;
    }

    int recv_size = H->total_recv_size;

    double *stage = GD->D.stage_centroid_values;
    double *xmom = GD->D.xmom_centroid_values;
    double *ymom = GD->D.ymom_centroid_values;
    double *recv_buf = H->recv_buffer;
    int *flat_recv = H->flat_recv_indices;

    // Wait for all Irecv + Isend requests posted in begin
    MPI_Waitall(2 * H->num_neighbors, H->requests, MPI_STATUSES_IGNORE);

    // --- H2D copy + unpack ---
#ifdef GPU_AWARE_MPI
    if (GD->device_id >= 0) {
        double *host_recv = H->host_recv_buffer;
        int host = omp_get_initial_device();
        int dev  = omp_get_default_device();

        // Copy received halo data from host staging buffer to device
        omp_target_memcpy(recv_buf, host_recv,
                          3 * recv_size * sizeof(double),
                          0, 0, dev, host);

        // Unpack on GPU
        #pragma omp target teams distribute parallel for is_device_ptr(recv_buf)
        for (int idx = 0; idx < recv_size; idx++) {
            int k = flat_recv[idx];
            stage[k] = recv_buf[3*idx + 0];
            xmom[k]  = recv_buf[3*idx + 1];
            ymom[k]  = recv_buf[3*idx + 2];
        }
    } else {
        // CPU-only: unpack directly from host recv_buf
        for (int idx = 0; idx < recv_size; idx++) {
            int k = flat_recv[idx];
            stage[k] = recv_buf[3*idx + 0];
            xmom[k]  = recv_buf[3*idx + 1];
            ymom[k]  = recv_buf[3*idx + 2];
        }
    }
#else
    // Non-GPU-aware: H2D then unpack on GPU
    if (GD->device_id >= 0) {
        #pragma omp target update to(recv_buf[0:3*recv_size])
    }

    OMP_PARALLEL_LOOP
    for (int idx = 0; idx < recv_size; idx++) {
        int k = flat_recv[idx];
        stage[k] = recv_buf[3*idx + 0];
        xmom[k]  = recv_buf[3*idx + 1];
        ymom[k]  = recv_buf[3*idx + 2];
    }
#endif
    NVTX_POP();
}

// gpu_exchange_ghosts: convenience wrapper — begin + end with no work between
// them.  Callers that want to overlap GPU computation with the MPI transfer
// should call gpu_exchange_ghosts_begin / gpu_exchange_ghosts_end directly
// with their GPU work scheduled between the two calls.
void gpu_exchange_ghosts(struct gpu_domain *GD) {
    NVTX_PUSH("gpu_exchange_ghosts");
    gpu_exchange_ghosts_begin(GD);
    gpu_exchange_ghosts_end(GD);
    NVTX_POP();
}

