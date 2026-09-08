# Inputs for `turbo verify --inputs`: one case per mandelbrot row, the same grid
# the bench uses. The first yield is the description line verify prints.

W, H, IT = 160, 120, 64


def cases():
    yield "%d rows x %d px, %d iterations, both versions run on the host" % (H, W, IT)
    dx = (3 << 12) // W
    for r in range(H):
        yield (bytearray(W), W, dx, ((r * 2) << 12) // H - (1 << 12), IT)
