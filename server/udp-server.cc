// -*- mode: c++; c-basic-offset: 4; indent-tabs-mode: nil; -*-
//
// This program is free software; you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation version 2.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// You should have received a copy of the GNU General Public License
// along with this program.  If not, see <http://gnu.org/licenses/gpl-2.0.txt>

#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <limits.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

#include "composite-flaschen-taschen.h"
#include "ft-thread.h"
#include "servers.h"
#include "ppm-reader.h"

volatile bool interrupt_received = false;
static void InterruptHandler(int signo) {
  interrupt_received = true;
}

// public interface
static int server_socket = -1;
static const char kSizeQueryMarker[] = "#FT:SIZE?";
static const char kSizeResponseFormat[] = "#FT:SIZE %d %d\n";

static bool IsSizeQuery(const char *buffer, ssize_t len) {
    const size_t marker_len = strlen(kSizeQueryMarker);
    if (len < (ssize_t) marker_len) return false;
    for (ssize_t pos = 0; pos <= len - (ssize_t) marker_len; ++pos) {
        if (memcmp(buffer + pos, kSizeQueryMarker, marker_len) == 0)
            return true;
    }
    return false;
}

bool udp_server_init(int port) {
    if ((server_socket = socket(PF_INET6, SOCK_DGRAM, IPPROTO_UDP)) < 0) {
        perror("IPv6 enabled ? While reating listen socket");
        return false;
    }
    int opt = 0;   // Unset IPv6-only, in case it is set. Best effort.
    setsockopt(server_socket, IPPROTO_IPV6, IPV6_V6ONLY, &opt, sizeof(opt));

    opt = 1;
    setsockopt(server_socket, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    struct sockaddr_in6 addr = {0};
    addr.sin6_family = AF_INET6;
    addr.sin6_addr = in6addr_any;
    addr.sin6_port = htons(port);
    if (bind(server_socket, (struct sockaddr *) &addr, sizeof(addr)) < 0) {
        perror("bind");
        return false;
    }

    fprintf(stderr, "UDP-server: ready to listen on %d\n", port);
    return true;
}

void udp_server_run_blocking(CompositeFlaschenTaschen *display,
                             ft::Mutex *mutex) {
    static const int kBufferSize = 65535;  // maximum UDP has to offer.
    char *packet_buffer = new char[kBufferSize];
    bzero(packet_buffer, kBufferSize);

    // Make sure the kernel keeps enough pending packets in case we have a
    // large display.
    const int kMinReceiveBuffer = 3 * 65535;
    const int kBufferMinimumFullFrameCount = 3;
    const int full_frame_data = display->width() * display->height() * 3 + 1024;
    int recv_size = kBufferMinimumFullFrameCount * full_frame_data;
    if (recv_size < kMinReceiveBuffer) {
      recv_size = kMinReceiveBuffer; // Small displays should have a minimum.
    }
    if (setsockopt(server_socket, SOL_SOCKET, SO_RCVBUF, //
                   &recv_size, sizeof(recv_size)) < 0) {
      fprintf(stderr,
              "Can not set a comfortable receive buffer size.\n"
              "Consider setting at least\n"
              "sudo sysctl -w net.core.rmem_max=%d\n",
              recv_size);
    }

    struct sigaction sa = {{0}};  // https://gcc.gnu.org/bugzilla/show_bug.cgi?id=53119
    sa.sa_handler = InterruptHandler;
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);

    for (;;) {
        struct sockaddr_storage client_addr;
        socklen_t client_addr_len = sizeof(client_addr);
        ssize_t received_bytes = recvfrom(server_socket,
                                          packet_buffer, kBufferSize,
                                          0,
                                          (struct sockaddr *) &client_addr,
                                          &client_addr_len);
        if (interrupt_received)
            break;

        if (received_bytes < 0 && errno == EINTR) // Other signals. Don't care.
            continue;

        if (received_bytes < 0) {
            perror("Trouble receiving.");
            break;
        }

        if (IsSizeQuery(packet_buffer, received_bytes)) {
            char response[64];
            const int response_len = snprintf(response, sizeof(response),
                                              kSizeResponseFormat,
                                              display->width(),
                                              display->height());
            if (sendto(server_socket, response, response_len, 0,
                       (struct sockaddr *) &client_addr,
                       client_addr_len) < 0) {
                perror("Trouble responding to display size query.");
            }
            continue;
        }

        ImageMetaInfo img_info = {0};
        img_info.width = display->width();  // defaults.
        img_info.height = display->height();

        const char *pixel_pos = ReadImageData(packet_buffer, received_bytes,
                                              &img_info);
        mutex->Lock();
        display->SetLayer(img_info.layer);
        for (int y = 0; y < img_info.height; ++y) {
            for (int x = 0; x < img_info.width; ++x) {
                Color c;
                c.r = *pixel_pos++;
                c.g = *pixel_pos++;
                c.b = *pixel_pos++;
                display->SetPixel(x + img_info.offset_x,
                                  y + img_info.offset_y,
                                  c);
            }
        }
        display->Send();
        display->SetLayer(0);  // Back to sane default.
        mutex->Unlock();
    }
    delete [] packet_buffer;
}
