/* Linux test-only interposer: pause at the first source hole punch, after the
 * store write but before cold-page registration. Never linked into pagerd. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

int fallocate(int fd, int mode, off_t offset, off_t length) {
    int (*real_fallocate)(int, int, off_t, off_t) = dlsym(RTLD_NEXT, "fallocate");
    const char *ready = getenv("CRATE_TEST_PUNCH_READY");
    const char *release = getenv("CRATE_TEST_PUNCH_RELEASE");
    if (ready && release && (mode & FALLOC_FL_PUNCH_HOLE)) {
        int marker = open(ready, O_CREAT | O_EXCL | O_WRONLY, 0600);
        if (marker >= 0) {
            close(marker);
            struct timespec delay = {.tv_nsec = 10000000};
            for (int n = 0; access(release, F_OK); n++) {
                if (n >= 1000) { errno = ETIMEDOUT; return -1; }
                nanosleep(&delay, NULL);
            }
        }
    }
    return real_fallocate(fd, mode, offset, length);
}
