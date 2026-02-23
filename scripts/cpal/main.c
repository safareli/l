/*
 * Fast parallel hard-link copy of a directory tree.
 *
 * Benchmarks (228K files, 35K dirs, 3.6GB, ext4, 7 cores):
 *
 *   cp -al (baseline)              2.43s
 *   Python (parallel cp -al)       1.70s
 *   C io_uring (7 workers)         1.38s  -- batching hurt by per-dir flush
 *   C lock-free, 1 worker          2.30s
 *   C lock-free, 4 workers         1.20s
 *   C lock-free, 7 workers         1.17s  <-- best real copy, 2x faster
 *   fuse-overlayfs                 0.02s  <-- instant, but CoW not real copy
 *
 * Key optimizations:
 *   - getdents64 syscall directly (skips libc readdir overhead)
 *   - linkat/mkdirat with fds (no path string building)
 *   - Atomic counter for task dispatch (zero lock contention)
 *   - Top-level dirs as tasks = natural load balancing
 *
 * Approaches tried but slower:
 *   - io_uring: batches linkat but must flush per-dir (FDs close), net loss
 *   - 3286 threads (one per top-level dir): thread spawn overhead > mutex cost
 *   - Python pure os.walk+os.link: interpreter overhead at 228K files
 *
 *
 * Usage: ./hardlink_copy <src> <dst> [nworkers]
 *   default nworkers = max(1, cpu_count - 1)
 */

#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/types.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

/* ---- getdents64 ---- */

struct linux_dirent64 {
    __u64          d_ino;
    __s64          d_off;
    unsigned short d_reclen;
    unsigned char  d_type;
    char           d_name[];
};

static inline int sys_getdents64(int fd, void *buf, unsigned len) {
    return syscall(SYS_getdents64, fd, buf, len);
}

/* ---- recursive hardlink (single dir, called per-thread) ---- */

static void hardlink_tree(int src_fd, int dst_fd) {
    char buf[32768];
    int n;

    /* first pass: hardlink all files */
    while ((n = sys_getdents64(src_fd, buf, sizeof(buf))) > 0) {
        int pos = 0;
        while (pos < n) {
            struct linux_dirent64 *d = (void *)(buf + pos);
            pos += d->d_reclen;
            if (d->d_type != DT_DIR)
                linkat(src_fd, d->d_name, dst_fd, d->d_name, 0);
        }
    }

    /* rewind */
    lseek(src_fd, 0, SEEK_SET);

    /* second pass: recurse into subdirs */
    while ((n = sys_getdents64(src_fd, buf, sizeof(buf))) > 0) {
        int pos = 0;
        while (pos < n) {
            struct linux_dirent64 *d = (void *)(buf + pos);
            pos += d->d_reclen;
            if (d->d_type != DT_DIR) continue;
            const char *name = d->d_name;
            if (name[0] == '.' && (name[1] == '\0' || (name[1] == '.' && name[2] == '\0')))
                continue;

            mkdirat(dst_fd, name, 0755);
            int sfd = openat(src_fd, name, O_RDONLY | O_DIRECTORY);
            int dfd = openat(dst_fd, name, O_RDONLY | O_DIRECTORY);
            if (sfd >= 0 && dfd >= 0)
                hardlink_tree(sfd, dfd);
            if (sfd >= 0) close(sfd);
            if (dfd >= 0) close(dfd);
        }
    }
}

/* ---- top-level parallel dispatch ---- */

typedef struct { int src_fd; int dst_fd; } task_t;

static task_t *tasks;
static int ntasks;
static atomic_int next_task = 0;

static void *worker(void *arg) {
    (void)arg;
    int i;
    while ((i = atomic_fetch_add(&next_task, 1)) < ntasks) {
        hardlink_tree(tasks[i].src_fd, tasks[i].dst_fd);
        close(tasks[i].src_fd);
        close(tasks[i].dst_fd);
    }
    return NULL;
}

int main(int argc, char **argv) {
    if (argc < 3) {
        fprintf(stderr, "Usage: %s <src> <dst> [nworkers]\n", argv[0]);
        return 1;
    }

    long cpu_count = sysconf(_SC_NPROCESSORS_ONLN);
    int default_workers = (cpu_count > 1) ? (int)(cpu_count - 1) : 1;

    int nworkers = (argc > 3) ? atoi(argv[3]) : default_workers;
    if (nworkers < 1) nworkers = 1;

    mkdir(argv[2], 0755);

    int src_fd = open(argv[1], O_RDONLY | O_DIRECTORY);
    int dst_fd = open(argv[2], O_RDONLY | O_DIRECTORY);
    if (src_fd < 0 || dst_fd < 0) { perror("open"); return 1; }

    /* collect top-level entries into task array */
    int cap = 4096;
    tasks = malloc(cap * sizeof(task_t));
    ntasks = 0;

    char buf[32768];
    int n;
    while ((n = sys_getdents64(src_fd, buf, sizeof(buf))) > 0) {
        int pos = 0;
        while (pos < n) {
            struct linux_dirent64 *d = (void *)(buf + pos);
            pos += d->d_reclen;
            const char *name = d->d_name;
            if (name[0] == '.' && (name[1] == '\0' || (name[1] == '.' && name[2] == '\0')))
                continue;

            if (d->d_type == DT_DIR) {
                mkdirat(dst_fd, name, 0755);
                int sfd = openat(src_fd, name, O_RDONLY | O_DIRECTORY);
                int dfd = openat(dst_fd, name, O_RDONLY | O_DIRECTORY);
                if (sfd >= 0 && dfd >= 0) {
                    if (ntasks >= cap) { cap *= 2; tasks = realloc(tasks, cap * sizeof(task_t)); }
                    tasks[ntasks++] = (task_t){sfd, dfd};
                } else {
                    if (sfd >= 0) close(sfd);
                    if (dfd >= 0) close(dfd);
                }
            } else {
                linkat(src_fd, name, dst_fd, name, 0);
            }
        }
    }
    close(src_fd);
    close(dst_fd);

    /* spawn workers — they grab tasks via atomic counter, no lock */
    pthread_t *tids = malloc(nworkers * sizeof(pthread_t));
    for (int i = 0; i < nworkers; i++)
        pthread_create(&tids[i], NULL, worker, NULL);
    for (int i = 0; i < nworkers; i++)
        pthread_join(tids[i], NULL);

    free(tids);
    free(tasks);
    return 0;
}
