import gc
gc.collect()
from cyd2usbr import CYD
import time
from ili9341 import color565
from gif_ili9341 import GifPlayer

cyd = CYD()

cyd.display.clear(cyd.BLACK)

gp = GifPlayer(cyd.display, chunk_lines=8)

gp.play("eye.gif", x=0, y=0, loop=True, bg_color=0)

