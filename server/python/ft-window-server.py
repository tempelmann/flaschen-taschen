#!/usr/bin/env python3
# -*- mode: python; c-basic-offset: 4; indent-tabs-mode: nil; -*-
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation version 2.

"""Display Flaschen Taschen UDP packets in an OpenCV window."""

import argparse
import select
import socket
import sys
from dataclasses import dataclass


DEFAULT_PORT = 1337
DEFAULT_DIMENSIONS = "384x256"
DEFAULT_SCALE = 2
LAYER_COUNT = 16
SIZE_QUERY_MARKER = b"#FT:SIZE?"
SIZE_RESPONSE = b"#FT:SIZE %d %d\n"


@dataclass
class ImagePacket:
    width: int
    height: int
    offset_x: int
    offset_y: int
    layer: int
    pixels: bytes


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
        self.layers = [
            bytearray(width * height * 3) for _ in range(LAYER_COUNT)
        ]
        self.visible = bytearray(width * height * 3)

        self.cv2.namedWindow(self.window_name, self.cv2.WINDOW_NORMAL)
        self.cv2.resizeWindow(
            self.window_name,
            self.width * self.scale,
            self.height * self.scale,
        )
        self.render()

    def close(self):
        self.socket.close()
        self.cv2.destroyAllWindows()

    def run(self):
        try:
            while True:
                if self.poll_socket():
                    self.render()
                key = self.cv2.waitKey(10) & 0xff
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

    def poll_socket(self):
        updated = False
        for _ in range(100):
            readable, _, _ = select.select([self.socket], [], [], 0)
            if not readable:
                break
            data, address = self.socket.recvfrom(65535)
            if SIZE_QUERY_MARKER in data:
                self.socket.sendto(SIZE_RESPONSE % (self.width, self.height), address)
                continue
            try:
                packet = parse_packet(data, self.width, self.height)
            except ValueError as err:
                print("Ignoring packet: %s" % err, file=sys.stderr)
                continue
            self.draw_packet(packet)
            updated = True

        return updated

    def draw_packet(self, packet):
        layer_index = max(0, min(LAYER_COUNT - 1, packet.layer))
        layer = self.layers[layer_index]
        source = 0
        for y in range(packet.height):
            target_y = y + packet.offset_y
            if target_y < 0 or target_y >= self.height:
                source += packet.width * 3
                continue
            for x in range(packet.width):
                target_x = x + packet.offset_x
                if 0 <= target_x < self.width:
                    target = (target_y * self.width + target_x) * 3
                    layer[target : target + 3] = packet.pixels[source : source + 3]
                source += 3

    def compose(self):
        visible = self.visible
        background = self.layers[0]
        visible[:] = background
        for layer in self.layers[1:]:
            for pos in range(0, len(visible), 3):
                if layer[pos] or layer[pos + 1] or layer[pos + 2]:
                    visible[pos : pos + 3] = layer[pos : pos + 3]

    def render(self):
        self.compose()
        rgb = self.np.frombuffer(self.visible, dtype=self.np.uint8).reshape(
            (self.height, self.width, 3)
        )
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


def main():
    args = parse_args()
    width, height = args.dimension
    server = WindowServer(width, height, args.scale, args.host, args.port)
    print("UDP window server listening on port %d for %dx%d" %
          (args.port, width, height))
    server.run()


if __name__ == "__main__":
    main()
