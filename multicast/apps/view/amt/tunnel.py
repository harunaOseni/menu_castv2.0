import socket
import struct
import sys
import logging
from scapy.all import send, IP, UDP, Packet
from scapy.contrib.igmpv3 import IGMPv3, IGMPv3gr, IGMPv3mr
import secrets
from datetime import datetime
import time
import psutil
import random

from constants import DEFAULT_MTU, LOCAL_LOOPBACK, MCAST_ALLHOSTS, MCAST_ANYCAST
from models import (
    AMT_Discovery,
    AMT_Relay_Request,
    AMT_Membership_Query,
    AMT_Membership_Update,
    AMT_Multicast_Data,
)

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename="amt_tunnel.log",
    filemode="a",
)
logger = logging.getLogger(__name__)

# Add a stream handler to also log to console
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
logger.addHandler(console_handler)

# Define the default relay and its IP addresses
DEFAULT_RELAY = "amt-relay.m2icast.net"
DEFAULT_RELAY_IPS = ["162.250.137.254", "162.250.136.101", "164.113.199.110"]


def setup_socket(amt_port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", amt_port))
    s.settimeout(60)  # Set a timeout for receiving data
    return s


def send_amt_discovery(ip_layer, udp_layer, nonce):
    amt_layer = AMT_Discovery()
    amt_layer.setfieldval("nonce", nonce)
    discovery_packet = ip_layer / udp_layer / amt_layer
    send(discovery_packet)
    logger.info(
        f"Sent AMT relay discovery to {ip_layer.dst}:2268 with nonce {nonce.hex()}"
    )


def send_amt_request(ip_layer, udp_layer, nonce):
    amt_layer = AMT_Relay_Request()
    amt_layer.setfieldval("nonce", nonce)
    request_packet = ip_layer / udp_layer / amt_layer
    send(request_packet)
    logger.info(
        f"Sent AMT relay request to {ip_layer.dst}:2268 with nonce {nonce.hex()}"
    )


def send_membership_update(ip_layer, udp_layer, nonce, response_mac, multicast, source):
    amt_layer = AMT_Membership_Update()
    amt_layer.setfieldval("nonce", nonce)
    amt_layer.setfieldval("response_mac", response_mac)

    options_pkt = Packet(b"\x00")
    ip_layer2 = IP(src=MCAST_ANYCAST, dst=MCAST_ALLHOSTS, options=[options_pkt])

    igmp_layer = IGMPv3()
    igmp_layer.type = 34

    igmp_layer2 = IGMPv3mr(records=[IGMPv3gr(maddr=multicast, srcaddrs=[source])])

    membership_update_packet = (
        ip_layer / udp_layer / amt_layer / ip_layer2 / igmp_layer / igmp_layer2
    )
    send(membership_update_packet)
    logger.info(
        f"Sent AMT multicast membership update to {ip_layer.dst}:2268 for group {multicast} from source {source}"
    )


def monitor_resources():
    cpu_percent = psutil.cpu_percent()
    memory_percent = psutil.virtual_memory().percent
    if cpu_percent > 90 or memory_percent > 90:
        logger.warning(
            f"High resource usage: CPU {cpu_percent}%, Memory {memory_percent}%"
        )
    return cpu_percent, memory_percent


def get_relay_ip(relay):
    if relay == DEFAULT_RELAY:
        return random.choice(DEFAULT_RELAY_IPS)
    return relay


def setup_amt_tunnel(relay, amt_port, multicast, source):
    s = setup_socket(amt_port)
    logger.info(f"Socket set up on port {amt_port}")

    relay_ip = get_relay_ip(relay)
    ip_layer = IP(dst=relay_ip)
    udp_layer = UDP(sport=amt_port, dport=2268)
    nonce = secrets.token_bytes(4)

    send_amt_discovery(ip_layer, udp_layer, nonce)

    try:
        data, addr = s.recvfrom(8192)
        logger.info(f"Received {len(data)} bytes from relay {addr}")
    except socket.timeout:
        logger.error("Timeout: Did not receive any response from the relay")
        return None, None, None, None, None
    except Exception as e:
        logger.error(f"Failed to receive data from relay: {e}")
        return None, None, None, None, None

    send_amt_request(ip_layer, udp_layer, nonce)

    try:
        data, addr = s.recvfrom(DEFAULT_MTU)
        membership_query = AMT_Membership_Query(data)
        response_mac = membership_query.response_mac
        logger.info(
            f"Received AMT multicast membership query from {addr} with response MAC {response_mac.hex() if isinstance(response_mac, bytes) else response_mac}"
        )
    except Exception as e:
        logger.error(f"Failed to receive or process membership query: {e}")
        return None, None, None, None, None

    req = struct.pack("=4sl", socket.inet_aton(multicast), socket.INADDR_ANY)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, req)

    send_membership_update(ip_layer, udp_layer, nonce, response_mac, multicast, source)
    return s, ip_layer, udp_layer, nonce, response_mac


def main(relay, source, multicast, amt_port, udp_port):
    logger.info(
        f"Starting AMT tunnel - Relay: {relay}, Source: {source}, Multicast: {multicast}, AMT Port: {amt_port}, UDP Port: {udp_port}"
    )

    if relay == "amt-relay.m2icast.net":
        logger.info("Using default relay: amt-relay.m2icast.net")
    elif relay not in DEFAULT_RELAY_IPS:
        logger.warning(f"Non-default relay specified: {relay}")

    packet_count = 0
    last_packet_time = time.time()
    buffer = []
    max_buffer_size = 100
    reconnect_attempts = 0
    max_reconnect_attempts = 5
    local_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)

    s, ip_layer, udp_layer, nonce, response_mac = setup_amt_tunnel(
        relay, amt_port, multicast, source
    )
    if s is None:
        logger.error("Failed to set up initial AMT tunnel. Exiting.")
        sys.exit(1)

    last_resource_check = time.time()
    resource_check_interval = 60
    high_resource_count = 0
    max_high_resource_count = 5

    while True:
        try:
            data, _ = s.recvfrom(DEFAULT_MTU)
            amt_packet = AMT_Multicast_Data(data)
            raw_udp = bytes(amt_packet[UDP].payload)

            buffer.append(raw_udp)
            if len(buffer) > max_buffer_size:
                buffer.pop(0)

            local_socket.sendto(raw_udp, (LOCAL_LOOPBACK, udp_port))

            packet_count += 1
            last_packet_time = time.time()
            reconnect_attempts = 0

            if packet_count % 1000 == 0:
                logger.info(f"Received and forwarded {packet_count} packets")

        except socket.timeout:
            current_time = time.time()
            if current_time - last_packet_time > 10:
                logger.warning(
                    "No data received for 10 seconds, attempting to reconnect"
                )
                reconnect_attempts += 1
                if reconnect_attempts > max_reconnect_attempts:
                    logger.error(
                        "Max reconnection attempts reached. Trying a different relay IP."
                    )
                    relay = DEFAULT_RELAY  # Force using a random default relay IP
                    reconnect_attempts = 0

                s, ip_layer, udp_layer, nonce, response_mac = setup_amt_tunnel(
                    relay, amt_port, multicast, source
                )
                if s is not None:
                    logger.info(
                        f"AMT tunnel re-established successfully with relay {ip_layer.dst}"
                    )
                    last_packet_time = time.time()
                else:
                    logger.error(
                        f"Failed to re-establish AMT tunnel with relay {ip_layer.dst}"
                    )

        except Exception as err:
            logger.error(f"Error occurred in processing packet: {err}")

        if time.time() - last_packet_time > 30:
            try:
                heartbeat_packet = ip_layer / udp_layer / AMT_Membership_Update()
                send(heartbeat_packet)
                logger.debug("Sent heartbeat packet")
            except Exception as e:
                logger.error(f"Failed to send heartbeat: {e}")

        if time.time() - last_resource_check > resource_check_interval:
            cpu_percent, memory_percent = monitor_resources()
            last_resource_check = time.time()

            if cpu_percent > 90 or memory_percent > 90:
                high_resource_count += 1
                if high_resource_count >= max_high_resource_count:
                    logger.error(
                        f"Consistently high resource usage detected. CPU: {cpu_percent}%, Memory: {memory_percent}%. Attempting to restart AMT tunnel."
                    )
                    relay = DEFAULT_RELAY  # Force using a random default relay IP
                    s, ip_layer, udp_layer, nonce, response_mac = setup_amt_tunnel(
                        relay, amt_port, multicast, source
                    )
                    if s is not None:
                        logger.info(
                            f"AMT tunnel restarted successfully with relay {ip_layer.dst} due to high resource usage"
                        )
                        high_resource_count = 0
                    else:
                        logger.error(
                            f"Failed to restart AMT tunnel with relay {ip_layer.dst} after high resource usage"
                        )
                        break
            else:
                high_resource_count = 0

        time.sleep(0.001)

    logger.info("Exiting AMT tunnel")
    s.close()
    local_socket.close()


if __name__ == "__main__":
    if len(sys.argv) != 6:
        print(
            "Usage: python tunnel.py <relay> <source> <multicast> <amt_port> <udp_port>"
        )
        sys.exit(1)

    relay, source, multicast = sys.argv[1:4]
    amt_port, udp_port = map(int, sys.argv[4:6])

    main(relay, source, multicast, amt_port, udp_port)
