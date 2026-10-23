/*
 * prctl_guard: post-execve credential guard and loopback connect reporter.
 *
 * 1. PR_SET_DUMPABLE is reset to 1 by execve(2), so a pre-exec hook cannot
 *    leave the Pi CLI child non-dumpable. An LD_PRELOAD constructor runs after
 *    exec, inside the new process, and applies the flag there. Every process Pi
 *    spawns (bash/tools) also inherits LD_PRELOAD and applies it again.
 *
 * 2. The Pi runner must still tell which loopback TCP connections belong to the
 *    Pi process. Since the Pi process is now non-dumpable, the runner cannot
 *    inspect /proc/<pi>/fd. This library therefore interposes connect(2) and
 *    reports (pid, local port) for every successful loopback connection to the
 *    runner over an inherited pipe FD (single <=PIPE_BUF atomic write). The
 *    runner only allows connections whose reported pid is the spawned Pi pid;
 *    a shell that connects without the interposer (or unsets LD_PRELOAD) gets
 *    no registration and is rejected. FD_CLOEXEC is set on the report pipe in
 *    the constructor so exec'd tool children cannot inherit (or forge) it.
 *
 * Keep this file dependency-free and cheap; it is preloaded into every Pi
 * child process.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <errno.h>
#include <sys/socket.h>
#include <unistd.h>

#define REPORT_FD_ENV "HARNESS_PRCTL_GUARD_REPORT_FD"

static int report_fd = -1;
static int (*real_connect)(int, const struct sockaddr *, socklen_t) = NULL;

static int is_loopback(const struct sockaddr *address, socklen_t length) {
    if (address == NULL) {
        return 0;
    }
    if (address->sa_family == AF_INET && length >= (socklen_t)sizeof(struct sockaddr_in)) {
        const struct sockaddr_in *ipv4 = (const struct sockaddr_in *)address;
        uint32_t host = ntohl(ipv4->sin_addr.s_addr);
        return (host >> 24) == 127;
    }
    if (address->sa_family == AF_INET6 && length >= (socklen_t)sizeof(struct sockaddr_in6)) {
        static const unsigned char loopback[16] = {0, 0, 0, 0, 0, 0, 0, 0,
                                                   0, 0, 0, 0, 0, 0, 0, 1};
        const struct sockaddr_in6 *ipv6 = (const struct sockaddr_in6 *)address;
        return memcmp(ipv6->sin6_addr.s6_addr, loopback, 16) == 0;
    }
    return 0;
}

static void report_connected_port(int fd) {
    struct sockaddr_storage local;
    socklen_t length = (socklen_t)sizeof(local);
    uint32_t record[3];
    if (report_fd < 0) {
        return;
    }
    if (getsockname(fd, (struct sockaddr *)&local, &length) != 0) {
        return;
    }
    record[0] = (uint32_t)getpid();
    if (local.ss_family == AF_INET) {
        record[1] = (uint32_t)ntohs(((struct sockaddr_in *)&local)->sin_port);
    } else if (local.ss_family == AF_INET6) {
        record[1] = (uint32_t)ntohs(((struct sockaddr_in6 *)&local)->sin6_port);
    } else {
        return;
    }
    record[2] = (uint32_t)local.ss_family;
    /* 12 bytes: one atomic write, well below PIPE_BUF. */
    {
        ssize_t written = write(report_fd, record, sizeof(record));
        (void)written;
    }
}

int connect(int fd, const struct sockaddr *address, socklen_t length) {
    int result;
    if (real_connect == NULL) {
        real_connect = (int (*)(int, const struct sockaddr *, socklen_t))
            dlsym(RTLD_NEXT, "connect");
    }
    if (real_connect == NULL) {
        return -1;
    }
    result = real_connect(fd, address, length);
    /* Non-blocking sockets return EINPROGRESS; the source port is already
     * assigned, so report the connection attempt in both success cases. */
    if (report_fd >= 0 && is_loopback(address, length)
            && (result == 0 || (result == -1 && errno == EINPROGRESS))) {
        report_connected_port(fd);
    }
    return result;
}

__attribute__((constructor)) static void prctl_guard_constructor(void) {
    const char *configured;
    /* Best effort: a hardened environment should still start if prctl is
     * unavailable, because credential hygiene also relies on env scrubbing. */
    (void)prctl(PR_SET_DUMPABLE, 0, 0, 0, 0);
    configured = getenv(REPORT_FD_ENV);
    if (configured != NULL && configured[0] != '\0') {
        int candidate = atoi(configured);
        int flags = (candidate >= 0) ? fcntl(candidate, F_GETFD) : -1;
        if (flags >= 0) {
            /* Inherited only by the Pi CLI process; FD_CLOEXEC keeps exec'd
             * bash/tool children from inheriting (or forging) the pipe. */
            report_fd = candidate;
            (void)fcntl(report_fd, F_SETFD, flags | FD_CLOEXEC);
        }
    }
}
