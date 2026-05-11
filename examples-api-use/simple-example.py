# (Hey Scotty, how can I set the 'include' path for python ?)
import flaschen
import time

# Getting "Message too long"? Need to fix the max UDP size – see client/README.md

UDP_IP = 'localhost'
UDP_PORT = 1337

N = 256

ft = flaschen.Flaschen(UDP_IP, UDP_PORT, N, N)

while True:
  for b in range(0, 256):
    for y in range(0, ft.height):
      for x in range(0, ft.width):
        ft.set(x, y, ((x * 255) // N, (y * 255) // N, b))
    ft.send()
    #time.sleep(0.003)
