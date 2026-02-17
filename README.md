# gif_ili9341
ili9341 GIF player

```from ili9341 import Display
from gif_ili9341 import GifPlayer

# --- set up your display --- #

gp = GifPlayer(display, chunk_lines=8)
gp.play("eye.gif", x=0, y=0, loop=True, bg_color=0)```