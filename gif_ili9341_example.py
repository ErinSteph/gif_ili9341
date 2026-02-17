from machine import Pin, SPI
from ili9341 import Display
from gif_ili9341 import GifPlayer

spi = SPI(1, baudrate=40_000_000, polarity=0, phase=0, sck=Pin(10), mosi=Pin(11), miso=Pin(12))
cs  = Pin(9)
dc  = Pin(8)
rst = Pin(13)

tft = Display(spi, cs=cs, dc=dc, rst=rst, width=240, height=320, rotation=0)
gp = GifPlayer(tft, chunk_lines=8)

gp.play("eye.gif", x=0, y=0, loop=True, bg_color=0)
