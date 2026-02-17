# gif_ili9341.py
# MicroPython GIF87a/89a decoder + player for ILI9341 Display.block()
#
# Usage:
#   from gif_ili9341 import GifPlayer
#   gp = GifPlayer(display)
#   gp.play("/flash/anim.gif", x=0, y=0, loop=True)
#
# Notes:
# - Transparency: draws transparent pixels as bg_color (default 0 / black)
# - Disposal methods: ignored
# - Renders in scanline "chunks" to reduce RAM use

import time
import gc

def _color565(r, g, b):
    return (r & 0xF8) << 8 | (g & 0xFC) << 3 | (b >> 3)

class _BitStream:
    __slots__ = ("data", "pos", "bitbuf", "bitcnt")
    def __init__(self, data):
        self.data = data
        self.pos = 0
        self.bitbuf = 0
        self.bitcnt = 0

    def read_bits(self, n):
        # Little-endian bit order in GIF LZW stream
        while self.bitcnt < n:
            if self.pos >= len(self.data):
                return None
            self.bitbuf |= self.data[self.pos] << self.bitcnt
            self.pos += 1
            self.bitcnt += 8
        out = self.bitbuf & ((1 << n) - 1)
        self.bitbuf >>= n
        self.bitcnt -= n
        return out

def _read_u16(f):
    b = f.read(2)
    return b[0] | (b[1] << 8)

def _skip_sub_blocks(f):
    while True:
        n = f.read(1)[0]
        if n == 0:
            return
        f.read(n)

def _read_sub_blocks_bytes(f):
    chunks = []
    total = 0
    while True:
        n_b = f.read(1)
        if not n_b:
            break
        n = n_b[0]
        if n == 0:
            break
        c = f.read(n)
        chunks.append(c)
        total += n
    if not chunks:
        return b""
    if len(chunks) == 1:
        return chunks[0]
    # join
    out = bytearray(total)
    p = 0
    for c in chunks:
        out[p:p+len(c)] = c
        p += len(c)
    return out

def _lzw_decode(min_code_size, data, expected_pixels=None):
    """
    Returns a list/bytearray of color indices (0..255).
    """
    clear_code = 1 << min_code_size
    end_code = clear_code + 1
    code_size = min_code_size + 1
    next_code = end_code + 1
    max_code_size = 12

    # dictionary maps code -> bytes of indices
    # Initialize dictionary with single-byte entries
    dict_entries = [bytes([i]) for i in range(clear_code)]
    dict_entries.append(b"")  # clear (placeholder)
    dict_entries.append(b"")  # end (placeholder)

    bs = _BitStream(data)

    out = bytearray()
    prev = None

    def reset_dict():
        nonlocal dict_entries, code_size, next_code, prev
        dict_entries = [bytes([i]) for i in range(clear_code)] + [b"", b""]
        code_size = min_code_size + 1
        next_code = end_code + 1
        prev = None

    reset_dict()

    while True:
        code = bs.read_bits(code_size)
        if code is None:
            break

        if code == clear_code:
            reset_dict()
            continue
        if code == end_code:
            break

        if code < len(dict_entries) and dict_entries[code]:
            entry = dict_entries[code]
        elif code == next_code and prev is not None:
            # KwKwK case
            entry = prev + prev[:1]
        else:
            # corrupted stream
            break

        out.extend(entry)
        if expected_pixels is not None and len(out) >= expected_pixels:
            # We can stop once we have enough pixels
            out = out[:expected_pixels]
            break

        if prev is not None:
            dict_entries.append(prev + entry[:1])
            next_code += 1
            # grow code size when crossing boundary
            if next_code == (1 << code_size) and code_size < max_code_size:
                code_size += 1

        prev = entry

        # GIF dict never exceeds 4096
        if next_code >= 4096:
            # wait for clear_code
            pass

    return out

class GifPlayer:
    def __init__(self, display, chunk_lines=8):
        """
        display: your ILI9341 Display instance
        chunk_lines: number of lines per write block (RAM vs speed tradeoff)
        """
        self.d = display
        self.chunk_lines = chunk_lines

    def _read_header_and_lsd(self, f):
        hdr = f.read(6)
        if hdr not in (b"GIF87a", b"GIF89a"):
            raise ValueError("Not a GIF")

        width = _read_u16(f)
        height = _read_u16(f)
        packed = f.read(1)[0]
        bg_index = f.read(1)[0]
        f.read(1)  # pixel aspect

        gct_flag = (packed >> 7) & 1
        gct_size_pow = (packed & 0x07)  # size = 2^(N+1)
        gct_size = 1 << (gct_size_pow + 1) if gct_flag else 0

        gct = None
        if gct_flag:
            gct_raw = f.read(3 * gct_size)
            gct = gct_raw

        return width, height, bg_index, gct

    def _palette_to_565(self, pal_raw):
        # pal_raw: bytes of RGB triplets
        # returns list of 16-bit ints
        if pal_raw is None:
            return None
        n = len(pal_raw) // 3
        out = [0] * n
        j = 0
        for i in range(n):
            r = pal_raw[j]; g = pal_raw[j+1]; b = pal_raw[j+2]
            j += 3
            out[i] = _color565(r, g, b)
        return out

    def _iter_frames(self, f):
        """
        Yields dict per frame:
          {
            "x","y","w","h",
            "delay_ms",
            "trans_index" or None,
            "palette_565": list[int],
            "indices": bytearray length w*h,
            "interlace": bool
          }
        """
        # Graphic Control Extension state applies to next image
        gce_delay_ms = 0
        gce_trans_index = None

        while True:
            b = f.read(1)
            if not b:
                return
            sep = b[0]

            if sep == 0x3B:
                # Trailer
                return

            if sep == 0x21:
                # Extension
                label = f.read(1)[0]
                if label == 0xF9:
                    # Graphic Control Extension
                    block_size = f.read(1)[0]  # should be 4
                    if block_size != 4:
                        f.read(block_size)
                        _skip_sub_blocks(f)
                        continue
                    packed = f.read(1)[0]
                    delay_cs = _read_u16(f)  # centiseconds
                    trans = f.read(1)[0]
                    f.read(1)  # block terminator 0

                    transparency_flag = packed & 0x01
                    gce_trans_index = trans if transparency_flag else None
                    gce_delay_ms = delay_cs * 10
                else:
                    # Skip other extensions (Application, Comment, PlainText, etc.)
                    _skip_sub_blocks(f)
                continue

            if sep == 0x2C:
                # Image Descriptor
                ix = _read_u16(f)
                iy = _read_u16(f)
                iw = _read_u16(f)
                ih = _read_u16(f)
                packed = f.read(1)[0]
                lct_flag = (packed >> 7) & 1
                interlace = (packed >> 6) & 1
                lct_size_pow = (packed & 0x07)
                lct_size = 1 << (lct_size_pow + 1) if lct_flag else 0

                lct_raw = None
                if lct_flag:
                    lct_raw = f.read(3 * lct_size)

                lzw_min = f.read(1)[0]
                img_data = _read_sub_blocks_bytes(f)

                expected = iw * ih
                indices = _lzw_decode(lzw_min, img_data, expected_pixels=expected)

                frame = {
                    "x": ix, "y": iy, "w": iw, "h": ih,
                    "delay_ms": gce_delay_ms,
                    "trans_index": gce_trans_index,
                    "lct_raw": lct_raw,
                    "indices": indices,
                    "interlace": bool(interlace),
                }

                # Reset GCE for next frame per spec behavior
                gce_delay_ms = 0
                gce_trans_index = None

                yield frame
                continue

            # Unknown byte; bail
            return

    def _interlace_rows(self, h):
        # GIF interlace pattern
        # pass 1: 0,8,16...
        # pass 2: 4,12,20...
        # pass 3: 2,6,10...
        # pass 4: 1,3,5...
        rows = []
        for r in range(0, h, 8): rows.append(r)
        for r in range(4, h, 8): rows.append(r)
        for r in range(2, h, 4): rows.append(r)
        for r in range(1, h, 2): rows.append(r)
        return rows

    def draw_frame(self, frame, x=0, y=0, bg_color=0, palette_565=None):
        """
        Draw a single decoded frame dict at (x,y) offset on the display.
        palette_565: palette to use (local overrides global)
        """
        d = self.d
        fx = x + frame["x"]
        fy = y + frame["y"]
        w = frame["w"]
        h = frame["h"]
        idx = frame["indices"]
        trans = frame["trans_index"]

        # Choose row order for interlaced frames
        if frame["interlace"]:
            row_map = self._interlace_rows(h)
        else:
            row_map = None

        cl = self.chunk_lines
        if cl <= 0:
            cl = 1
        # ensure chunk_lines divides nicely or handle remainder
        # We build chunk buffer as RGB565 big-endian bytes for display.block()
        # Buffer size: w * chunk_h * 2 bytes
        # Keep it small (default 8 lines)

        # Pre-allocate one chunk buffer at max size
        max_chunk_h = cl
        buf = bytearray(w * max_chunk_h * 2)

        # Render in chunks
        y_out = 0
        while y_out < h:
            chunk_h = max_chunk_h
            if y_out + chunk_h > h:
                chunk_h = h - y_out

            # Fill chunk buffer
            bi = 0
            for cy in range(chunk_h):
                src_row = (row_map[y_out + cy] if row_map else (y_out + cy))
                row_start = src_row * w
                for cx in range(w):
                    pi = idx[row_start + cx]
                    if (trans is not None) and (pi == trans):
                        c = bg_color
                    else:
                        # palette lookup with bounds clamp
                        if pi < len(palette_565):
                            c = palette_565[pi]
                        else:
                            c = bg_color
                    buf[bi] = (c >> 8) & 0xFF
                    buf[bi + 1] = c & 0xFF
                    bi += 2

            # Push to display
            d.block(fx, fy + y_out, fx + w - 1, fy + y_out + chunk_h - 1,
                    memoryview(buf)[: w * chunk_h * 2])

            y_out += chunk_h

        gc.collect()

    def play(self, path, x=0, y=0, loop=True, bg_color=0, max_loops=None):
        """
        Plays a GIF from filesystem path.

        loop: True to loop forever (or until max_loops), False to play once
        max_loops: optional int to stop after N loops (useful for testing)
        """
        loops = 0
        while True:
            with open(path, "rb") as f:
                screen_w, screen_h, bg_index, gct_raw = self._read_header_and_lsd(f)
                gpal_565 = self._palette_to_565(gct_raw)

                # If bg_color not explicitly set, you can use GIF background index:
                # but only if GCT exists. We'll keep caller's bg_color as priority.
                # (Uncomment if you want)
                # if bg_color == 0 and gpal_565 and bg_index < len(gpal_565):
                #     bg_color = gpal_565[bg_index]

                for frame in self._iter_frames(f):
                    lct_raw = frame["lct_raw"]
                    pal_565 = self._palette_to_565(lct_raw) if lct_raw else gpal_565
                    if pal_565 is None:
                        # No palette? can't draw
                        continue

                    t0 = time.ticks_ms()
                    self.draw_frame(frame, x=x, y=y, bg_color=bg_color, palette_565=pal_565)

                    delay = frame["delay_ms"]
                    # GIF "0 delay" is common; clamp to a tiny delay so it doesn't peg CPU
                    if delay <= 0:
                        delay = 10

                    # keep time more accurate than sleep(delay/1000) if draw time is significant
                    dt = time.ticks_diff(time.ticks_ms(), t0)
                    remaining = delay - dt
                    if remaining > 0:
                        time.sleep_ms(remaining)

            loops += 1
            if not loop:
                return
            if max_loops is not None and loops >= max_loops:
                return
