
#define _GNU_SOURCE
#include <sys/ptrace.h>
#include <sys/wait.h>
#include <sys/uio.h>
#include <linux/elf.h>
#include <asm/ptrace.h>
#include <stdio.h>
#include <unistd.h>
#include <signal.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>

int main(int argc, char **argv) {
    pid_t pid = fork();
    if (pid == 0) {
        ptrace(PTRACE_TRACEME, 0, 0, 0);
        execl(argv[1], argv[1], "--version", (char *)0);
        _exit(127);
    }
    int st;
    waitpid(pid, &st, 0);
    ptrace(PTRACE_CONT, pid, 0, 0);
    waitpid(pid, &st, 0);
    if (WIFSTOPPED(st)) {
        int sig = WSTOPSIG(st);
        struct user_pt_regs regs;
        struct iovec iov = { &regs, sizeof(regs) };
        if (ptrace(PTRACE_GETREGSET, pid, (void *)NT_PRSTATUS, &iov) == 0) {
            printf("stop_signal=%d (%s)\n", sig, strsignal(sig));
            printf("pc=0x%llx sp=0x%llx lr=0x%llx\n",
                   (unsigned long long)regs.pc,
                   (unsigned long long)regs.sp,
                   (unsigned long long)regs.regs[30]);
            errno = 0;
            long insn = ptrace(PTRACE_PEEKTEXT, pid, (void *)regs.pc, 0);
            printf("insn_word=0x%08lx (errno=%d)\n", (unsigned long)insn & 0xffffffffUL, errno);
        } else {
            perror("GETREGSET");
        }
    }
    return 0;
}
