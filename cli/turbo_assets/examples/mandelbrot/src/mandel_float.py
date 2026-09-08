# The version anyone would write first: floating point, no decorator, plain
# bytecode. Same signature as src/pixels.py so `turbo verify` can call both with
# the same arguments and show what the fixed-point rewrite did to the answer.


def mandel_row(out, width, dx, cy, max_iter):
    fdx = dx / 4096.0
    fcy = cy / 4096.0
    for px in range(width):
        cx = px * fdx - 2.0
        x = 0.0
        y = 0.0
        i = 0
        while i < max_iter:
            x2 = x * x
            y2 = y * y
            if x2 + y2 > 4.0:
                break
            y = 2.0 * x * y + fcy
            x = x2 - y2 + cx
            i += 1
        out[px] = i


def _turbo_bench():
    W, H, IT = 160, 120, 64
    row = bytearray(W)
    dx = (3 << 12) // W
    total = 0
    for r in range(H):
        mandel_row(row, W, dx, ((r * 2) << 12) // H - (1 << 12), IT)
        total += sum(row)
    return total
