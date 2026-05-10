Python Window Server
====================

This is a small development server that implements the Flaschen Taschen UDP
protocol and displays incoming frames in an OpenCV window.

Install the window dependency:

```bash
python3 -m pip install opencv-python
```

Run it from the repository root:

```bash
python3 server/python/ft-window-server.py -D384x256
```

Options:

```
-D, --dimension WIDTHxHEIGHT  display size in pixels, default 384x256
-p, --port PORT               UDP port, default 1337
-s, --scale SCALE             integer window scale factor, default 2
--host ADDRESS                local address to bind, default all interfaces
```

The server understands `#FT:` offsets/layers and replies to `#FT:SIZE?`, so
clients can use their `-G` option to discover the configured dimensions.
