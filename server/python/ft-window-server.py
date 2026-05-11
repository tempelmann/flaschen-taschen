#!/usr/bin/env python3
# -*- mode: python; c-basic-offset: 4; indent-tabs-mode: nil; -*-
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation version 2.

"""Display Flaschen Taschen UDP packets in an OpenCV window."""

import argparse
import queue
import socket
import sys
import threading
import time
from dataclasses import dataclass


DEFAULT_PORT = 1337
DEFAULT_DIMENSIONS = "384x256"
DEFAULT_SCALE = 2
LAYER_COUNT = 16
SIZE_QUERY_MARKER = b"#FT:SIZE?"
SIZE_RESPONSE = b"#FT:SIZE %d %d\n"
PARTIAL_FRAME_TIMEOUT_SECONDS = 0.1


@dataclass
class ImagePacket:
    width: int
    height: int
    offset_x: int
    offset_y: int
    layer: int
    pixels: bytes


class FullFrameCandidate:
    def __init__(self, width, height, layer):
        self.width = width
        self.height = height
        self.layer = layer
        self.pixels = bytearray(width * height * 3)
        self.covered_rows = bytearray(height)
        self.covered_count = 0
        self.first_seen = time.monotonic()
        self.last_seen = self.first_seen

    def add(self, packet):
        row_size = self.width * 3
        for row in range(packet.height):
            target_y = packet.offset_y + row
            if target_y < 0 or target_y >= self.height:
                continue
            source = row * row_size
            target = target_y * row_size
            self.pixels[target : target + row_size] = packet.pixels[
                source : source + row_size
            ]
            if not self.covered_rows[target_y]:
                self.covered_rows[target_y] = 1
                self.covered_count += 1
        self.last_seen = time.monotonic()

    def is_complete(self):
        return self.covered_count == self.height

    def complete_packet(self):
        return ImagePacket(
            width=self.width,
            height=self.height,
            offset_x=0,
            offset_y=0,
            layer=self.layer,
            pixels=bytes(self.pixels),
        )

    def partial_packets(self):
        row_size = self.width * 3
        packets = []
        row = 0
        while row < self.height:
            while row < self.height and not self.covered_rows[row]:
                row += 1
            start = row
            while row < self.height and self.covered_rows[row]:
                row += 1
            if start == row:
                continue
            packets.append(
                ImagePacket(
                    width=self.width,
                    height=row - start,
                    offset_x=0,
                    offset_y=start,
                    layer=self.layer,
                    pixels=bytes(self.pixels[start * row_size : row * row_size]),
                )
            )
        return packets


class FullFrameAssembler:
    def __init__(self, width, height, layer_count, partial_timeout):
        self.width = width
        self.height = height
        self.layer_count = layer_count
        self.partial_timeout = partial_timeout
        self.pending = {}

    def add(self, packet):
        if not self._looks_like_full_width_stripe(packet):
            return [packet]

        layer = max(0, min(self.layer_count - 1, packet.layer))
        candidate = self.pending.get(layer)

        if candidate is not None and self._overlaps(candidate, packet):
            # Without a frame id, overlapping rows are the strongest signal that
            # a new frame has started before the previous one completed.
            del self.pending[layer]
            candidate = None

        if candidate is None:
            candidate = FullFrameCandidate(self.width, self.height, layer)
            self.pending[layer] = candidate

        candidate.add(packet)
        if candidate.is_complete():
            del self.pending[layer]
            return [candidate.complete_packet()]
        return []

    def flush_stale(self):
        now = time.monotonic()
        ready = []
        for layer, candidate in list(self.pending.items()):
            if now - candidate.last_seen < self.partial_timeout:
                continue
            ready.extend(candidate.partial_packets())
            del self.pending[layer]
        return ready

    def _looks_like_full_width_stripe(self, packet):
        return (
            packet.width == self.width
            and packet.offset_x == 0
            and packet.height > 0
            and packet.offset_y < self.height
            and packet.offset_y + packet.height > 0
        )

    def _overlaps(self, candidate, packet):
        start = max(0, packet.offset_y)
        end = min(self.height, packet.offset_y + packet.height)
        for row in range(start, end):
            if candidate.covered_rows[row]:
                return True
        return False


def parse_dimensions(value):
    try:
        width_text, height_text = value.lower().split("x", 1)
        width = int(width_text)
        height = int(height_text)
    except ValueError:
        raise argparse.ArgumentTypeError("expected <width>x<height>")
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("dimensions must be positive")
    return width, height


def parse_args():
    parser = argparse.ArgumentParser(
        description="Show Flaschen Taschen UDP packets in a window."
    )
    parser.add_argument(
        "-D",
        "--dimension",
        default=DEFAULT_DIMENSIONS,
        type=parse_dimensions,
        metavar="WIDTHxHEIGHT",
        help="display dimensions in pixels (default: %(default)s)",
    )
    parser.add_argument(
        "-p",
        "--port",
        default=DEFAULT_PORT,
        type=int,
        help="UDP port to listen on (default: %(default)s)",
    )
    parser.add_argument(
        "-s",
        "--scale",
        default=DEFAULT_SCALE,
        type=int,
        help="integer window scale factor (default: %(default)s)",
    )
    parser.add_argument(
        "--host",
        default="",
        help="local address to bind (default: all interfaces)",
    )
    args = parser.parse_args()
    if args.port <= 0 or args.port > 65535:
        parser.error("port must be in range 1..65535")
    if args.scale <= 0:
        parser.error("scale must be positive")
    return args


def parse_ft_comment(line, packet):
    fields = line[4:].strip().split()
    if not fields:
        return
    if fields[0].startswith(b"SIZE"):
        return
    try:
        packet.offset_x = int(fields[0])
        if len(fields) > 1:
            packet.offset_y = int(fields[1])
        if len(fields) > 2:
            packet.layer = int(fields[2])
    except ValueError:
        pass


def next_token(data, pos, packet):
    length = len(data)
    while True:
        while pos < length and chr(data[pos]).isspace():
            pos += 1
        if pos >= length:
            raise ValueError("unexpected end of packet")
        if data[pos] != ord("#"):
            break
        line_end = data.find(b"\n", pos)
        if line_end < 0:
            raise ValueError("unterminated comment")
        line = data[pos:line_end]
        if line.startswith(b"#FT:"):
            parse_ft_comment(line, packet)
        pos = line_end + 1

    end = pos
    while end < length and not chr(data[end]).isspace():
        end += 1
    return data[pos:end], end


def skip_single_whitespace(data, pos):
    if pos >= len(data) or not chr(data[pos]).isspace():
        raise ValueError("missing whitespace before image data")
    return pos + 1


def parse_footer(data, pos, packet):
    footer = data[pos:].split()
    try:
        if len(footer) > 0:
            packet.offset_x = int(footer[0])
        if len(footer) > 1:
            packet.offset_y = int(footer[1])
        if len(footer) > 2:
            packet.layer = int(footer[2])
    except ValueError:
        pass


def parse_packet(data, display_width, display_height):
    packet = ImagePacket(
        width=display_width,
        height=display_height,
        offset_x=0,
        offset_y=0,
        layer=0,
        pixels=data,
    )
    if (
        len(data) < 3
        or data[:2] != b"P6"
        or (not chr(data[2]).isspace() and data[2] != ord("#"))
    ):
        expected = display_width * display_height * 3
        if len(data) < expected:
            raise ValueError("raw packet is too small")
        packet.pixels = data[:expected]
        return packet

    pos = 2
    width_token, pos = next_token(data, pos, packet)
    height_token, pos = next_token(data, pos, packet)
    range_token, pos = next_token(data, pos, packet)
    pos = skip_single_whitespace(data, pos)

    packet.width = int(width_token)
    packet.height = int(height_token)
    max_value = int(range_token)
    if packet.width <= 0 or packet.height <= 0 or max_value != 255:
        raise ValueError("unsupported PPM header")

    expected = packet.width * packet.height * 3
    if len(data) - pos < expected:
        raise ValueError("packet image data is too small")

    packet.pixels = data[pos : pos + expected]
    parse_footer(data, pos + expected, packet)
    return packet


class WindowServer:
    def __init__(self, width, height, scale, host, port):
        try:
            import cv2
            import numpy as np
        except ImportError as err:
            raise SystemExit(
                "OpenCV is required for the window server. Install it with "
                "`python3 -m pip install opencv-python`: %s" % err
            )
        self.cv2 = cv2
        self.np = np
        self.width = width
        self.height = height
        self.scale = scale
        self.window_name = "Flaschen Taschen %dx%d UDP:%d" % (width, height, port)
        self.socket = open_socket(host, port)
        configure_receive_buffer(self.socket, width, height)
        self.socket.settimeout(0.1)
        self.incoming_packets = queue.SimpleQueue()
        self.running = threading.Event()
        self.running.set()
        self.receiver = threading.Thread(target=self.receive_packets, daemon=True)
        self.frame_assembler = FullFrameAssembler(
            width, height, LAYER_COUNT, PARTIAL_FRAME_TIMEOUT_SECONDS
        )
        self.layers = self.np.zeros(
            (LAYER_COUNT, height, width, 3), dtype=self.np.uint8
        )
        self.visible = self.np.zeros((height, width, 3), dtype=self.np.uint8)

        self.cv2.namedWindow(self.window_name, self.cv2.WINDOW_NORMAL)
        self.cv2.resizeWindow(
            self.window_name,
            self.width * self.scale,
            self.height * self.scale,
        )
        self.render()

    def close(self):
        self.running.clear()
        self.socket.close()
        if self.receiver.is_alive():
            self.receiver.join(timeout=1.0)
        self.cv2.destroyAllWindows()

    def run(self):
        try:
            self.receiver.start()
            while True:
                if self.apply_pending_packets():
                    self.render()
                key = self.cv2.waitKey(1) & 0xff
                if key == 27 or key == ord("q"):
                    break
                try:
                    if self.cv2.getWindowProperty(
                        self.window_name, self.cv2.WND_PROP_VISIBLE
                    ) < 1:
                        break
                except self.cv2.error:
                    break
        finally:
            self.close()

    def receive_packets(self):
        while self.running.is_set():
            try:
                data, address = self.socket.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break

            if SIZE_QUERY_MARKER in data:
                try:
                    self.socket.sendto(
                        SIZE_RESPONSE % (self.width, self.height), address
                    )
                except OSError as err:
                    print(
                        "Trouble responding to display size query: %s" % err,
                        file=sys.stderr,
                    )
                continue

            try:
                packet = parse_packet(data, self.width, self.height)
            except ValueError as err:
                print("Ignoring packet: %s" % err, file=sys.stderr)
                continue
            self.incoming_packets.put(packet)

    def apply_pending_packets(self):
        updated = False
        while True:
            try:
                packet = self.incoming_packets.get_nowait()
            except queue.Empty:
                break

            for ready_packet in self.frame_assembler.add(packet):
                self.draw_packet(ready_packet)
                updated = True

        for ready_packet in self.frame_assembler.flush_stale():
            self.draw_packet(ready_packet)
            updated = True

        return updated

    def draw_packet(self, packet):
        layer_index = max(0, min(LAYER_COUNT - 1, packet.layer))
        target_x0 = max(0, packet.offset_x)
        target_y0 = max(0, packet.offset_y)
        target_x1 = min(self.width, packet.offset_x + packet.width)
        target_y1 = min(self.height, packet.offset_y + packet.height)
        if target_x0 >= target_x1 or target_y0 >= target_y1:
            return

        source_x0 = target_x0 - packet.offset_x
        source_y0 = target_y0 - packet.offset_y
        source_x1 = source_x0 + (target_x1 - target_x0)
        source_y1 = source_y0 + (target_y1 - target_y0)
        source = self.np.frombuffer(packet.pixels, dtype=self.np.uint8).reshape(
            (packet.height, packet.width, 3)
        )
        self.layers[
            layer_index, target_y0:target_y1, target_x0:target_x1, :
        ] = source[source_y0:source_y1, source_x0:source_x1, :]

    def compose(self):
        self.visible[...] = self.layers[0]
        for layer in self.layers[1:]:
            mask = self.np.any(layer != 0, axis=2)
            self.visible[mask] = layer[mask]

    def render(self):
        self.compose()
        rgb = self.visible
        if self.scale != 1:
            rgb = self.cv2.resize(
                rgb,
                (self.width * self.scale, self.height * self.scale),
                interpolation=self.cv2.INTER_NEAREST,
            )
        bgr = self.cv2.cvtColor(rgb, self.cv2.COLOR_RGB2BGR)
        self.cv2.imshow(self.window_name, bgr)


def open_socket(host, port):
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        sock.bind((host, port))
    except OSError:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((host, port))
    sock.setblocking(False)
    return sock


def configure_receive_buffer(sock, width, height):
    min_receive_buffer = 3 * 65535
    full_frame_data = width * height * 3 + 1024
    receive_buffer = max(min_receive_buffer, 3 * full_frame_data)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer)
    except OSError as err:
        print(
            "Can not set UDP receive buffer to %d bytes: %s"
            % (receive_buffer, err),
            file=sys.stderr,
        )


def main():
    args = parse_args()
    width, height = args.dimension
    server = WindowServer(width, height, args.scale, args.host, args.port)
    print("UDP window server listening on port %d for %dx%d" %
          (args.port, width, height))
    server.run()


if __name__ == "__main__":
    main()
