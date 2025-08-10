# (Hey Scotty, how can I set the 'include' path for python ?)
import flaschen
import socket

UDP_IP = 'localhost'
UDP_PORT = 1337

W = 256
H = 384

ft = flaschen.Flaschen(UDP_IP, UDP_PORT, W, H, 5, False, socket.SOCK_STREAM)

while True:
  for b in range(0, 256):
    for y in range(0, ft.height):
      for x in range(0, ft.width):
        ft.set(x, y, ((x * 255) // W, (y * 255) // H, b))
    ft.send()
