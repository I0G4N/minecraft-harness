/* Install before Node starts: deny filesystem mutation and new processes.
 * This is one layer inside a read-only, cap-dropped, isolated container. It is
 * intentionally not a JavaScript allowlist or a complete standalone sandbox.
 */
#define _GNU_SOURCE
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <sched.h>
#include <stddef.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <unistd.h>

#if defined(__x86_64__)
#define EXPECTED_ARCH AUDIT_ARCH_X86_64
#elif defined(__aarch64__)
#define EXPECTED_ARCH AUDIT_ARCH_AARCH64
#else
#error Unsupported architecture; fail closed rather than ship an unfiltered worker
#endif
#define DENY(nr) BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K, (nr), 0, 1), BPF_STMT(BPF_RET|BPF_K, SECCOMP_RET_ERRNO|EPERM)
#define ARG_FLAGS(syscallnum,arg) BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,(syscallnum),0,4), BPF_STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args[(arg)])), BPF_JUMP(BPF_JMP|BPF_JSET|BPF_K,O_WRONLY|O_RDWR|O_CREAT|O_TRUNC|O_APPEND,0,1), BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|EPERM), BPF_STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,nr))
__attribute__((constructor)) static void confine(void) {
  struct sock_filter code[] = {
    BPF_STMT(BPF_LD|BPF_W|BPF_ABS, offsetof(struct seccomp_data,arch)),
    BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K, EXPECTED_ARCH, 1, 0),
    BPF_STMT(BPF_RET|BPF_K, SECCOMP_RET_KILL_PROCESS),
    BPF_STMT(BPF_LD|BPF_W|BPF_ABS, offsetof(struct seccomp_data,nr)),
#ifdef __x86_64__
    /* Reject x32 syscall aliases; otherwise they bypass native syscall IDs. */
    BPF_JUMP(BPF_JMP|BPF_JSET|BPF_K, 0x40000000, 0, 1),
    BPF_STMT(BPF_RET|BPF_K, SECCOMP_RET_ERRNO|EPERM),
#endif
    DENY(__NR_execve), DENY(__NR_execveat),
#ifdef __NR_fork
    DENY(__NR_fork),
#endif
#ifdef __NR_vfork
    DENY(__NR_vfork),
#endif
    /* glibc retries clone when clone3 returns ENOSYS. Only threads may clone. */
#ifdef __NR_clone3
    BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,__NR_clone3,0,1),
    BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|ENOSYS),
#endif
    BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,__NR_clone,0,4),
    BPF_STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args[0])),
    BPF_JUMP(BPF_JMP|BPF_JSET|BPF_K,CLONE_THREAD,1,0),
    BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|EPERM),
    BPF_STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,nr)),
#ifdef __NR_open
    ARG_FLAGS(__NR_open,1),
#endif
    ARG_FLAGS(__NR_openat,2),
#ifdef __NR_openat2
    DENY(__NR_openat2),
#endif
#ifdef __NR_creat
    DENY(__NR_creat),
#endif
    DENY(__NR_truncate), DENY(__NR_ftruncate), DENY(__NR_fallocate),
#ifdef __NR_unlink
    DENY(__NR_unlink),
#endif
    DENY(__NR_unlinkat),
#ifdef __NR_rename
    DENY(__NR_rename),
#endif
    DENY(__NR_renameat), DENY(__NR_renameat2),
#ifdef __NR_mkdir
    DENY(__NR_mkdir),
#endif
    DENY(__NR_mkdirat),
#ifdef __NR_rmdir
    DENY(__NR_rmdir),
#endif
#ifdef __NR_link
    DENY(__NR_link), DENY(__NR_symlink),
#endif
    DENY(__NR_linkat), DENY(__NR_symlinkat),
#ifdef __NR_mknod
    DENY(__NR_mknod),
#endif
    DENY(__NR_mknodat),
#ifdef __NR_chmod
    DENY(__NR_chmod), DENY(__NR_chown), DENY(__NR_lchown),
#endif
    DENY(__NR_fchmod), DENY(__NR_fchmodat), DENY(__NR_fchown), DENY(__NR_fchownat),
    DENY(__NR_setxattr), DENY(__NR_lsetxattr), DENY(__NR_fsetxattr),
    DENY(__NR_removexattr), DENY(__NR_lremovexattr), DENY(__NR_fremovexattr),
#ifdef __NR_utime
    DENY(__NR_utime), DENY(__NR_utimes),
#endif
    DENY(__NR_utimensat),
    DENY(__NR_mount), DENY(__NR_umount2), DENY(__NR_pivot_root), DENY(__NR_chroot),
    DENY(__NR_ptrace), DENY(__NR_process_vm_writev), DENY(__NR_memfd_create),
    DENY(__NR_bpf), DENY(__NR_userfaultfd), DENY(__NR_io_uring_setup),
#ifdef __NR_open_by_handle_at
    DENY(__NR_open_by_handle_at),
#endif
    BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_ALLOW),
  };
  struct sock_fprog filter = { .len = sizeof(code)/sizeof(code[0]), .filter = code };
  if (prctl(PR_SET_NO_NEW_PRIVS,1,0,0,0) || prctl(PR_SET_SECCOMP,SECCOMP_MODE_FILTER,&filter)) {
    const char text[] = "sandbox seccomp installation failed\n";
    ssize_t written = write(2,text,sizeof(text)-1); (void)written; _exit(125);
  }
  if (setenv("HARNESS_NATIVE_SANDBOX", "seccomp-v1", 1)) _exit(125);
}
