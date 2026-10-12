"""Private network-namespace packet test of the production nftables rule."""
import json
import os
from pathlib import Path
import selectors
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from troubleshooter_host.host_io import command
from troubleshooter_host.inputs import encoded
from troubleshooter_host.network import BRIDGE, firewall_objects, validate_firewall


def run(*args, stdin=b""):
    return command(list(args), stdin=stdin, timeout=15)


def peer_address(pid, *args):
    return run("nsenter", "--target", str(pid), "--net", "ip", *args)


def peer():
    process = subprocess.Popen(["unshare", "--net", "sleep", "120"])
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("peer namespace failed")
        if os.readlink("/proc/{}/ns/net".format(process.pid)) != os.readlink("/proc/self/ns/net"):
            return process
        time.sleep(0.01)
    process.terminate()
    process.wait(timeout=5)
    raise RuntimeError("peer namespace unavailable")


def serve():
    with selectors.DefaultSelector() as selector:
        sockets = []
        try:
            for kind, ports in [(socket.SOCK_STREAM, (80, 443)), (socket.SOCK_DGRAM, (53, 123))]:
                for port in ports:
                    sock = socket.socket(socket.AF_INET, kind)
                    sockets.append(sock)
                    sock.bind(("169.254.169.254", port))
                    if kind == socket.SOCK_STREAM:
                        sock.listen()
                    selector.register(sock, selectors.EVENT_READ, kind)
            print("ready", flush=True)
            while True:
                for key, _ in selector.select(2):
                    if key.data == socket.SOCK_STREAM:
                        connection, _ = key.fileobj.accept()
                        connection.close()
                    else:
                        data, address = key.fileobj.recvfrom(32)
                        key.fileobj.sendto(data, address)
        finally:
            for sock in sockets:
                sock.close()


def probe(deny):
    for port in (80, 443):
        blocked = False
        try:
            with socket.create_connection(("169.254.169.254", port), timeout=1):
                pass
        except TimeoutError:
            blocked = True
        if blocked != deny:
            raise RuntimeError("metadata HTTP guard")
    for port in (53, 123):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(2)
            sock.sendto(b"synthetic", ("169.254.169.254", port))
            if sock.recv(32) != b"synthetic":
                raise RuntimeError("metadata DNS/NTP preservation")


def check():
    if os.readlink("/proc/self/ns/net") == os.readlink("/proc/1/ns/net"):
        raise RuntimeError("Refusing the host network namespace")
    processes = []
    try:
        source = peer()
        processes.append(source)
        target = peer()
        processes.append(target)
        run("ip", "link", "set", "lo", "up")
        run("ip", "link", "add", BRIDGE, "type", "bridge")
        run("ip", "address", "add", "10.89.42.1/24", "dev", BRIDGE)
        run("ip", "link", "set", BRIDGE, "up")
        for host, remote, process, local, address in [
            ("srcveth", "source0", source, None, "10.89.42.2/24"),
            ("outveth", "target0", target, "10.89.99.1/30", "10.89.99.2/30"),
        ]:
            run("ip", "link", "add", host, "type", "veth", "peer", "name", remote)
            run("ip", "link", "set", remote, "netns", str(process.pid))
            if local:
                run("ip", "address", "add", local, "dev", host)
            else:
                run("ip", "link", "set", host, "master", BRIDGE)
            run("ip", "link", "set", host, "up")
            peer_address(process.pid, "link", "set", "lo", "up")
            peer_address(process.pid, "address", "add", address, "dev", remote)
            peer_address(process.pid, "link", "set", remote, "up")
        peer_address(source.pid, "route", "add", "default", "via", "10.89.42.1")
        peer_address(target.pid, "route", "add", "default", "via", "10.89.99.1")
        peer_address(target.pid, "address", "add", "169.254.169.254/32", "dev", "lo")
        run("ip", "route", "add", "169.254.169.254/32", "via", "10.89.99.2")
        run("sysctl", "-w", "net.ipv4.ip_forward=1")
        server = subprocess.Popen(["nsenter", "--target", str(target.pid), "--net", sys.executable,
                                   __file__, "serve"], stdout=subprocess.PIPE)
        processes.append(server)
        with selectors.DefaultSelector() as selector:
            selector.register(server.stdout, selectors.EVENT_READ)
            if not selector.select(5) or server.stdout.readline() != b"ready\n":
                raise RuntimeError("metadata fixture unavailable")
        probe(False)
        run("nsenter", "--target", str(source.pid), "--net", sys.executable, __file__, "allow")
        run("nft", "--json", "--file", "-", stdin=encoded({"nftables": [{"add": obj} for obj in firewall_objects()]}))
        validate_firewall(json.loads(run("nft", "--json", "list", "ruleset")))
        run("nsenter", "--target", str(source.pid), "--net", sys.executable, __file__, "deny")
        probe(False)
    finally:
        for process in reversed(processes):
            process.terminate()
            process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if mode == "serve":
        serve()
    elif mode in ("allow", "deny"):
        probe(mode == "deny")
    else:
        check()
